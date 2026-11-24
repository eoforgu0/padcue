"""グループ(一緒に開始したレーンの集まり)のサーバー側中核。

グループの寿命:
- 開始はすべてこのグループの開始である。レーン 1 本の開始も 1 台のグループとして
  始まる。画面のレーンのボタンも、一括操作の行も、コマンドも、同じ start_group を通る
- レーンは最後に開始されたグループにだけ属する。そのレーンが別のグループで開始されると、
  前のグループから抜ける。前のグループは残りのレーンで続き、レーンが残らなければ
  消える。グループは同時にいくつでも持てる
- 停止してもグループは消えない。動いているかはグループの欄では持たず、そのつど各レーンの
  状態から求める(_group_active)
- 登録情報から消えたレーンはグループから抜ける。残り 1 本になったグループは
  1 台のグループとして残る
- 停止しているグループは、人が解除できる(dissolve_group)。レーン 1 本ずつの
  グループに分かれる

止め方の契約(docs/specs/coupling.md §1):
- グループの 1 台が停止すれば、残りも停止する。ただし、人が画面かコマンドでレーンを
  指して押した止め方で停止したレーンは、そのレーンだけが停止する(トリガーにしない)。
  グループの見出しはグループの全レーンを指す
- 止め方は、停止した 1 台の止まり方で決まる。異常停止・停止は即時、区切りで
  停止は各自の次の区切り、完了は完了した 1 台と同じ周回数の周の終わり(もう過ぎて
  いれば今の周の終わり)である。同じ周回数のレーンは停止させず、自分で完了する
- 連動停止を始めた後も、停止したレーンが出るたびに、その止まり方に合う止め方で、
  まだ動いているレーンを停止させる。停止させるのは、その止め方が今の予約(なし、周の
  終わり、次の区切り)より早く停止するレーンだけである。早いのは即時、次の区切り、
  周の終わりの順である。連動停止で停止させたレーンと、自分で完了したレーンは
  トリガーにしない
- 人が画面かコマンドで停止させるときは、指したレーンへ送り終えるまで、停止したレーンを
  トリガーにしない。送り終えた後も、指したレーンが押した止め方で停止したことは
  トリガーにしない
- 「未検出」はトリガーにしない。応答がないだけで、停止したとは限らない。戻ったときに
  停止していれば、止まり方が分からなくても(実行の途中で装置が起動し直した)、
  異常停止と同じく即時に残りを停止させる
- 合流点で待っているレーンも対象にする。ただし「完了」の止め方は周の境だけの
  マスクなので、合流点(装置から見れば選択待ち)では停止しない。そのレーンは
  相手が来ないと分かってから先へ進み、周の終わりで停止する。だから合流点の
  待ち合わせは、その実行の終わりを記録した後も続ける。即時と区切りの止め方では、
  合流点を進めずにその場で停止させる(合流点は区切りでもある)
- 分岐の選択はこのモジュールでは行わない。人が選ぶか、条件を見る
  CondWatcher(condwatch.py)が選ぶ。ただし合流点は分岐ではなく待ち合わせ
  なので、揃ったかどうかを見て SELECT を送る(joinwait.py)
- ブラウザーではなくサーバーが持つ。画面を閉じても連動停止は続く

スレッド構成: 監視は専用スレッド 1 本(0.5 秒周期)。装置の状態は収集キャッシュ
(DeviceLink)を読むだけである。装置へ送るのは、停止(「区切りで停止」の予約を含む)、
合流点の SELECT と、その前に装置の記録を読む LOGS と受け取りの通知(ack)、周回数を変える
予約とその解除、その続きの開始だけである。記録を先に読むのは、待ちに入った行を、
揃って進めた行より前に並べるためである(_tick_joins)。
"""
from __future__ import annotations

import json
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field, replace

from . import binfmt, lapend, positions, proto, registry, timeline
from .client import DeviceError, failure_of, failure_text, firmware_problem
from .conditions import ConditionStore, validate_source, validate_video
from .devicepool import keep_only
from .files import write_file
from .joinwait import JoinWaiter
from .lapends import LapEnds
from .procmodel import INFINITE, LAPS_BAD, start_pos
from .project import ConfigError, Project
from .sources import (
    LANE_VIDEO_NAME,
    dshow_unknown,
    find_source_by_name,
    normalize_source_name,
    source_present,
    source_problem,
)
from .words import sentences, state_ja


def _not_ready_text(dev: str, st: dict) -> str:
    """開始できない装置の拒否の理由。状態は画面のバッジと同じ用語で示す。

    動いている装置には停止させることを、起動や更新の途中の装置には待つことを
    案内する(内部の状態名は出さない)。
    """
    state = st.get("state")
    if st.get("running") or st.get("awaiting") or state in (
            "RUNNING", "AWAITING"):
        now = state_ja("AWAITING" if state == "AWAITING" else "RUNNING")
        return f"{dev} は{now}です。停止してから開始してください。"
    if state in ("BOOT", "WIFI_CONNECTING", "OTA"):
        return (f"{dev} は{state_ja(state)}です。"
                "待機中になってから開始してください。")
    return f"{dev} が待機中ではありません。状態を確認してから開始してください。"


def _now() -> float:
    return time.time()


def _dshow_unknown(source: dict) -> str:
    """見つからなかった映像ソースを「ない」と判断できない理由(映像機器だけ)。"""
    return dshow_unknown() if source.get("kind") == "dshow" else ""


def _new_group_id(started_at: float, taken) -> str:
    """グループの識別子。先頭の g に、開始時刻の UNIX ミリ秒を続けた文字列である。

    生成のために状態を持ち越さなくてよく、時刻の順に並ぶ。runstate.json を
    消した後に作った識別子も、過去の実行記録の識別子と重ならない。同じ値が
    既にあるときは、1 ミリ秒ずつ足してずらす。
    """
    ms = int(started_at * 1000)
    while f"g{ms}" in taken:
        ms += 1
    return f"g{ms}"


def _lap_end_ok(le) -> bool:
    """runstate.json の周回数を変える予約(lap_end)の形が、読める形か。"""
    if not isinstance(le, dict) or not isinstance(le.get("mode"), str):
        return False
    plans = le.get("plans")
    if not isinstance(plans, dict) or not all(
            isinstance(v, list) and len(v) == 2
            and (v[0] is None or isinstance(v[0], int)) and isinstance(v[1], bool)
            for v in plans.values()):
        return False
    waited = le.get("waited") or {}
    notes = le.get("notes") or {}
    left = le.get("left") or {}
    if not isinstance(left, dict) or not all(
            isinstance(v, dict) and isinstance(v.get("plan"), list)
            and len(v["plan"]) == 2 for v in left.values()):
        return False
    late = le.get("late") or {}
    if not isinstance(late, dict) or not all(v in _HAND_MODES for v in late.values()):
        return False
    return (isinstance(le.get("script") or [], list)
            and isinstance(waited, dict)
            and all(isinstance(v, list) and len(v) == 3 for v in waited.values())
            and isinstance(le.get("since") or {}, dict)
            and isinstance(notes, dict)
            and all(isinstance(v, list) and all(isinstance(n, dict) for n in v)
                    for v in notes.values()))


def _restarted(st: dict | None) -> bool:
    """装置の状態 st が、電源を入れてから一度も実行していない装置のものか(止まり方が
    なく、動いていない)。実行の途中で装置が起動し直すと、こう読める。"""
    st = st or {}
    return not (st.get("stop_reason") or st.get("running") or st.get("awaiting"))


def _wait_end_ok(we) -> bool:
    """runstate.json のレーンの、周回の終え方の続きを待つ間の終わり(wait_end。
    lapends の drop_of)が、読める形か。"""
    def num(v) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    return (isinstance(we, dict) and num(we.get("at"))
            and isinstance(we.get("stop", ""), str)
            and (we.get("by") is None
                 or (isinstance(we["by"], list) and len(we["by"]) == 2
                     and all(isinstance(v, str) for v in we["by"])))
            and all(isinstance(we.get(k, False), bool)
                    for k in ("failed", "unread", "release")))


def _picks_ok(picks) -> bool:
    """runstate.json のレーンの、選択の記録(picks。positions.pick_of の形の並び)が、
    読める形か。"""
    def num(v) -> bool:
        return isinstance(v, int) and not isinstance(v, bool)
    return isinstance(picks, list) and len(picks) <= positions.PICKS_MAX and all(
        isinstance(p, dict) and isinstance(p.get("block"), str)
        and isinstance(p.get("path"), list)
        and all(isinstance(s, str) for s in p["path"])
        and isinstance(p.get("turns"), list) and all(num(n) for n in p["turns"])
        and num(p.get("arm")) and (p.get("lap") is None or num(p["lap"]))
        for p in picks)


# 連動停止の記録(linked_stop)の、装置 ID の並びの欄と、文字列の欄
_LINKED_LISTS = ("pending", "sent", "resend", "later", "cancelled", "sent_later")
_LINKED_TEXTS = ("cause", "reason", "why")


def _drop_bad_fields(ls: dict) -> None:
    """runstate.json の連動停止の記録 ls(置き換えた前の記録も同じ形)の欄のうち、
    読めない形のものを捨てる。読めない形のまま読むと、監視はそのグループで毎周期例外に
    なり(停止する周を比べる所、止め方を調べる所)、状態の取得も失敗する(画面へ渡す名前に
    置き換える所)。

    捨ててよいのは、読む所がどれも欄のないときの値を持つからである(装置 ID の並びは
    空の並び、文字列は空、停止する周 lap は None)。もとのレーン(cause)が空だと、停止した
    レーンを連動とは記録しない。もとの止まり方(reason)が空だと、止め方はその場での
    停止になる(_LINKED_MODE)。lap が None なら今の周の終わりで停止させる
    (_linked_need)。
    """
    for k in _LINKED_LISTS:
        if k in ls and not (isinstance(ls[k], list)
                            and all(isinstance(d, str) for d in ls[k])):
            ls.pop(k)
    for k in _LINKED_TEXTS:
        if k in ls and not isinstance(ls[k], str):
            ls.pop(k)
    if "lap" in ls and not (ls["lap"] is None or (isinstance(ls["lap"], int)
                                                  and not isinstance(ls["lap"], bool))):
        ls.pop("lap")


def _laps_of(item: dict) -> int | str:
    """開始の指定の周回数を、変換器が受け取る形にする。

    周回数は合計であり、無限は欄がないか None で表す。0 は使わない。
    「1 周だけ」と「停止するまで」を 1 つの数で表そうとすると、0 に特別な意味を
    持たせることになり、入力欄でも記録でも読み違えの元になるからである。
    """
    v = item.get("laps")
    if v is None or v == "":
        return "infinite"
    n = int(v)
    if n < 1:
        raise ValueError(LAPS_BAD)
    return n


def _start_of(item: dict):
    """開始の指定の開始位置を、変換器が受け取る形にする。先頭なら None。

    ブロックの識別子が空でも、先頭とは限らない。周の境で停止した位置は、手順
    全体を囲むくり返しの何回目かだけを持つ(block が空、turns が [5] など)。
    これを先頭に丸めると、5 周目から始めたつもりが 1 周目から実行される。
    """
    at = item.get("start")
    if not at:
        return None
    pos = start_pos(at.get("path"), at.get("block"), at.get("turns"),
                    at.get("after"), at.get("waited"))
    if not pos.path and not pos.block and all(k == 1 for k in pos.turns):
        return None
    return pos


def _lap_head_text(turns) -> str:
    """周の境の開始位置の呼び方(「5 周目の先頭」)。"""
    turns = list(turns or [])
    return f"{turns[0]} 周目の先頭" if turns else "先頭"


def _lap_heads(r) -> dict[int, int]:
    """変換の結果 r で、頭を通る経過フレーム数が分かる周。{周: 経過フレーム数}。

    周回数のくり返しの対応表の項目(複製)ごとに、その最初の周の頭は区切りの表にある
    (経過フレーム数は、装置と同じく実行の始まりから数える)。先頭から始めた実行の
    1 周目の頭は、くり返しの前に書いたブロックの後にある。位置から始めた実行の最初の
    項目は開始の周の途中から始まるので、その周は実行の始まり(0)から数える。
    くり返しを抜けた所の項目は、周の頭ではない。
    """
    out: dict[int, int] = {}
    for item in r.lap_map or []:
        if item.get("exit"):
            continue
        start = item.get("start")
        mark = Project.mark_at(r.marks, start)
        frame = 0 if start == 0 else (mark or {}).get("frame")
        if isinstance(frame, int):
            out[item["first"]] = frame
    return out


def _start_record(item: dict, marks: list) -> dict:
    """実行の記録に残す開始位置。種類(head・lap・label・wait)と見せる名前を付ける。

    周の境は、周回数のくり返しの先頭である。手順全体のくり返しなら block が空で、
    周回数のくり返しを書いた手順ならそのくり返しの識別子を持つ。どちらも
    「5 周目の先頭」と呼ぶ。周の境か選択待ちかは、変換の区切りの表で、その種類の
    区切りが持つ経路と識別子と照合して決める。選択待ちの位置から始めるのは、そこで
    停止したときだけである。ラベルと選択待ちは送られた名前を使う(画面は、停止した
    後に改名したラベルを今の名前で送る)。最後の周の終わりで停止した位置(周回数の
    くり返しの直後。after)も周の境として残し、送られた名前を使う。
    """
    if _start_of(item) is None:
        return {"kind": "head"}
    at = item.get("start") or {}
    path = [str(x) for x in at.get("path") or []]
    block = str(at.get("block") or "")
    turns = list(at.get("turns") or [])
    kinds = {(tuple(m.get("path") or []), m.get("block") or ""): m.get("kind")
             for m in marks if m.get("kind") != "label"}
    kind = kinds.get((tuple(path), block))
    if at.get("after"):
        # 最後の周の終わりで停止した位置(周回数のくり返しの直後)。画面が送った
        # 名前(「3 周目の終わり」)を使う。周回数は回番号に入っていない
        return {"kind": "lap", "name": str(at.get("name") or ""),
                "block": block, "path": path, "turns": turns, "after": True}
    if not block or kind == "lap":
        return {"kind": "lap", "name": _lap_head_text(turns),
                "block": block, "path": path, "turns": turns}
    return {"kind": "label" if kind is None else "wait",
            "name": str(at.get("name") or ""),
            "block": block, "path": path, "turns": turns}


# 実行が終わったと確定するまで、続けて停止して見える長さ(秒)。収集は毎秒なので、
# 1 回の取りこぼしで終わったと判定しない
_END_CONFIRM_S = 1.5

# 開始を指示してから、装置と通信し始めるまでに待てる長さ(秒)。超えたら始めずに
# 拒否する。
# 押してから始まるまでに時間がたつと、押した人は始まらなかったと思い、装置の電源を
# 切ったり、ほかの操作に移ったりする。その後で始めると、人が見ていない所で Switch の
# 操作が始まる。普通は、ほかの要求の後ろで待つのは長くても 1 秒ほどである
START_WAIT_S = 10.0


def _reject(msg: str, lanes: list[str] | None = None,
            field: str = "proc", failure: str = "") -> dict:
    """開始を拒否する応答を作る。

    lanes は原因になったレーンの装置名である。画面はこれを見て、どのレーンの
    入力欄を異常の状態にするかを決める。文の中の名前を読み取らせない
    ためにあるので、レーンを特定できない拒否(登録情報と一致しない等)では空にする。

    field は修正すべき欄である。手順が原因なら "proc"、開始位置が原因なら
    "start"、周回数が原因なら "laps"。装置が原因の拒否(待機中でない・見つから
    ない・転送できない)は空で、画面はどの欄も赤くしない。修正できる欄がないのに欄を
    赤くすると、修正する先を指し間違え、装置が復旧しても赤いまま残る。

    failure は装置の失敗の種類(client.failure_of)で、コマンドの終了コードに使う。
    """
    out = {"error": msg}
    if failure:
        out["failure"] = failure
    if lanes:
        out["lanes"] = list(lanes)
        if field:
            out["field"] = field
    return out


def _most_laps(passes, laps: int, least: int) -> int:
    """変換できる最も大きい周回数(least 以上、laps 未満)。分からなければ 0。

    passes(n) は周回数 n で変換できるか。周回数を増やしても総フレーム数は
    減らないので、変換できる周回数は least から続く。二分探索なので、変換は
    32 回ほどで済む。least は開始位置の周で、それより少ない周回数では始められない。
    """
    least = max(1, least)
    if least >= laps or not passes(least):
        return 0
    lo, hi = least, laps
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if passes(mid):
            lo = mid
        else:
            hi = mid
    return lo


def _rollback_text(started: list, failed: list) -> list[str]:
    """一部だけ開始した後始末(_rollback_started)の結果を伝える文。

    先に開始したレーンのうち、停止の予約を送れたレーンは次の区切りで停止する。
    送れなかったレーンは動き続けるので、停止させることを求める。
    """
    reserved = [d for d in started if d not in failed]
    return [f"先に開始した {'・'.join(reserved)} は次の区切りで停止します"
            if reserved else "",
            f"{'・'.join(failed)} に区切りで停止を予約できません。停止してください"
            if failed else ""]


def _note(msg: str) -> dict:
    """グループ全体へのメッセージ。

    開始の応答の warnings は {"dev", "line", "shown", "msg", "proc", "block",
    "path"} の並びである(shown はブロックの警告だけが持つ)。block はその警告が
    指すブロックの識別子で、識別子を持たないブロックでは空である
    (file-format.md §6-1)。path はそこまでの呼び出しの経路で、
    一番上の手順の中なら空である。dev が空の
    ものはグループ全体へのメッセージで、文の中ですべてのレーンを挙げている(「1P は
    3 周目、2P は 先頭」など)。画面はこれをレーンごとにくり返さず、押した
    ボタンのそばに 1 つだけ出す。line はブロックの通し番号(無効にしたブロックを
    数えない)、shown は人に見せる番号(開始した手順の列に見えている順。呼び出し先の
    中の警告は、そこへ入っていく呼び出しのブロックの番号。dsl.LintWarning)で、
    手順の中の位置を指せないものは line が 0 で、shown は 0 か、欄がない。
    """
    return {"dev": "", "line": 0, "msg": msg}


def _build_warning(p: dict, w: dict) -> dict:
    """レーン p の変換の警告 w を、開始の応答の warnings の形(_note)にする。

    block と path はそのまま渡す。画面がそのブロックへ移動するのに使う(block は
    識別子を持たないブロックでは空、path は一番上の手順の中なら空である)。
    """
    return {"dev": p["dev"], "line": w["line"], "shown": w.get("shown", 0),
            "msg": w["msg"], "proc": p["name"], "block": w.get("block", ""),
            "path": w.get("path") or []}


def _lane_note(w: dict) -> dict:
    """開始の応答の warnings の項目を、レーンの記録に残すメッセージの形にする。"""
    return {k: w[k] for k in ("line", "shown", "msg", "block", "path") if k in w}


def _join_label(name: str) -> str:
    """文の中で合流点を指す言い方。名前のない合流点を「合流点「」」と引用しない。"""
    return f"合流点「{name}」" if name else "名前のない合流点"


def _check_joins(plan: list[dict],
                 builds: dict) -> tuple[dict | None, list[str]]:
    """グループのレーンの合流点を比べる。

    合流点の対応付けは、名前と、周回数のくり返しの回と、その回の中で何度目かの
    3 つで決まる。片方にしかない名前や、1 周あたりの個数が違う形を
    開始してしまうと、相手のいない合流点が残り、相手がその回を越えるまで待つ
    ぶんだけ時刻が静かにずれる。

    選択待ちの選択肢の中にある合流点は、通る回数が選ばれた選択肢によって変わる。
    その名前は拒否せず、知らせるにとどめる(拒否すると、正しく書けている手順まで開始
    できなくなる)。「対象の回」で決まる選択肢は、変換と同じく回ごとに判定して
    数える(startpos.join_counts)。周回数のくり返しを指す「対象の回」で周ごとに
    回数が変わる名前は、周ごとに比べる。返すのは (拒否する応答, メッセージの並び)
    で、拒否しないときの応答は None である。

    周回数のくり返しの外(前と後)にある合流点は、別に比べる。キーの
    「周」がくり返しの中とは別の「周の外」になるので、中の数と混ぜると、片方が
    くり返しの前に、もう片方が中に置いた形を通してしまう。
    """
    if len(plan) < 2:
        return None, []        # 1 台のグループには比べる相手がいない
    notes: list[str] = []
    for outside in (False, True):
        bad = _check_join_span(plan, builds, outside, notes)
        if bad is not None:
            return bad, notes
    return None, notes


def _check_join_span(plan: list[dict], builds: dict, outside: bool,
                     notes: list[str]) -> dict | None:
    """1 周ぶんの合流点か、周回数のくり返しの外の合流点を比べる。

    拒否するときはその応答を返し、メッセージは notes に追加する。
    """
    def joins(dev: str, outer: bool) -> dict:
        b = builds[dev]
        return b.joins_outside if outer else b.joins

    # 周ごとの表で比べられるのは、すべてのレーンが表を持つときである
    # (「n 回ごと」で周期になる手順は表を持たない)。周回数のくり返しの外は
    # 周がないので、全体の数で比べる
    tables = None if outside else {p["dev"]: builds[p["dev"]].lap_joins
                                   for p in plan}
    if tables is not None and any(v is None for v in tables.values()):
        tables = None

    where = "周回数のくり返しの外にある" if outside else ""
    names: list[str] = []
    for p in plan:
        for name in joins(p["dev"], outside):
            if name not in names:
                names.append(name)
    for name in names:
        counts = {p["dev"]: joins(p["dev"], outside).get(name)
                  for p in plan}
        missing = [d for d, c in counts.items() if not c or not c["max"]]
        if missing and len(missing) < len(plan):
            has = [d for d in counts if d not in missing]
            if not name:
                # 名前のない合流点は、名前を付ける側(持っている側)を修正する
                return _reject(
                    f"{where}名前のない合流点が {'・'.join(has)} の手順にあります。"
                    "名前を付けて、ほかのレーンの手順の合流点と合わせてください。",
                    has)
            # 指定するのは、その名前を持たないレーンだけである。持っている
            # 側は文の中で伝わる。両方に表示を付けると、どちらを修正すればよいのか
            # 画面からは読めない
            if not outside and all(joins(d, True).get(name) for d in missing):
                # 足りない側は、同じ名前をくり返しの外に置いている。置き忘れ
                # ではなく位置の違いなので、そう書く
                return _reject(
                    f"合流点「{name}」が、{'・'.join(has)} の手順では毎周通る位置に"
                    f"あり、{'・'.join(missing)} の手順では周回数のくり返しの外に"
                    "あります。同じ位置に合わせてください。", missing)
            # 名前の綴りが違うのか、置き忘れなのかは分からないので、両方の
            # 修正方法を書く
            return _reject(
                f"{where}合流点「{name}」が、{'・'.join(has)} の手順にはあり、"
                f"{'・'.join(missing)} の手順にはありません。"
                f"{'・'.join(missing)} の手順に同じ名前の合流点を置くか、"
                f"{'・'.join(has)} の手順から削除してください。",
                missing)
        varying = [d for d, c in counts.items()
                   if c and c["min"] != c["max"]]
        if tables is not None:
            bad = _lap_mismatch(name, tables, {p["dev"]: builds[p["dev"]].laps
                                               for p in plan})
            if bad is None:
                continue
            if bad != "varying":
                return bad
            varying = list(counts)
        if varying and tables is None and not outside and not any(
                counts[d].get("choice", True) for d in varying):
            # 「n 回ごと」で周期になる手順(周ごとの表がない)で、周によって回数が
            # 変わるだけである。同じ手順を同じ周回数で動かすレーンどうしは、どの周
            # でも揃う。違う手順どうしは周ごとに比べられないので知らせる
            if len({(p.get("name"), builds[p["dev"]].laps) for p in plan}) == 1:
                continue
            spans = "、".join(f"{d} は {counts[d]['min']}〜{counts[d]['max']} 回"
                             for d in counts if counts[d])
            notes.append(f"{_join_label(name)}を通る回数が周によって変わるため、"
                         "ほかのレーンと合流できない周がある可能性があります"
                         f"({spans})")
            continue
        if varying:
            spans = "、".join(f"{d} は {counts[d]['min']}〜{counts[d]['max']} 回"
                             for d in counts if counts[d])
            notes.append(
                f"{where}{_join_label(name)}を通る回数が分岐の選択によって変わります"
                f"({spans})。ほかのレーンと合流できない"
                f"{'ことがあります' if outside else '周があります'}。")
            continue
        fixed = {d: c["max"] for d, c in counts.items() if c}
        if len(set(fixed.values())) > 1:
            spans = "、".join(f"{d} は {n} 回" for d, n in fixed.items())
            return _reject(
                f"{where}{_join_label(name)}を通る回数がレーン間で異なります"
                f"({spans})。{'' if outside else '1 周あたりの'}"
                "回数を合わせてください。", list(fixed))
    return None


def _lap_counts(table: list, lap: int) -> dict:
    """周ごとの表(startpos.join_counts)から、その周の {名前: [最小, 最大]} を返す。"""
    for item in table:
        n = item["count"]
        if item["first"] <= lap and (n == INFINITE or lap < item["first"] + n):
            return item["joins"]
    return {}


def _lap_mismatch(name: str, tables: dict, laps: dict):
    """合流点 name の 1 周あたりの回数を、周ごとにレーンどうしで比べる。

    周回数のくり返しを指す「対象の回」があると、周ごとに回数が変わる。どのレーンも
    同じ周に同じ回数なら合流できるので、何も返さない。選択待ちの選択で回数が
    変わる周があれば "varying" を、決まった回数が違う周があれば拒否する応答を返す。
    比べる周は、各レーンの表の区間の始まりである(区間の中では回数が
    変わらない)。どのレーンもある周までしか実行しないので、一番短い周回数までを見る。
    """
    ends = [n for n in laps.values() if n != INFINITE]
    last = min(ends) if ends else None
    starts = sorted({item["first"] for table in tables.values() for item in table
                     if last is None or item["first"] <= last})
    varying = False
    for lap in starts:
        got = {d: tuple(_lap_counts(table, lap).get(name, (0, 0)))
               for d, table in tables.items()}
        if any(lo != hi for lo, hi in got.values()):
            varying = True
            continue
        if len({lo for lo, _hi in got.values()}) > 1:
            spans = "、".join(f"{d} は {lo} 回" for d, (lo, _hi) in got.items())
            if all(len(table) == 1 for table in tables.values()):
                return _reject(
                    f"{_join_label(name)}を通る回数がレーン間で異なります"
                    f"({spans})。1 周あたりの回数を合わせてください。", list(got))
            return _reject(
                f"{_join_label(name)}を通る回数が、{lap} 周目にレーン間で異なります"
                f"({spans})。周ごとの回数を合わせてください。", list(got))
    return "varying" if varying else None


def _check_start_laps(plan: list[dict], builds: dict) -> list[str]:
    """開始位置の周がレーンで違うことを知らせる。

    拒否しない。相手より進んだ周から始めたい場面はあるので、判断は利用者に
    委ねる。比べるのは、変換が開始位置から求めた周(BuildResult.start_lap)だけ
    である。開始位置の回番号(turns)には、周の中のくり返しの回も並ぶので、
    そのまま比べると、同じ周の中で位置が違うだけのレーンまで違うと知らせる。
    """
    if len(plan) < 2:
        return []
    laps = {p["dev"]: builds[p["dev"]].start_lap for p in plan}
    if len(set(laps.values())) <= 1:
        return []
    shown = "、".join(
        f"{d} は" + ("周回数のくり返しの後ろ" if n is None else f" {n} 周目")
        for d, n in laps.items())
    return [f"レーンによって開始する周が違います({shown})。"
            "合流点は、相手がその周を越えるまで待ちます。"]


# 一括操作の停止を送れなかったレーンの文と、最後に付ける次にできること。止め方ごとに、
# 送ろうとしたことを書く(コマンドの出力と同じ動詞。cli.py の stop)
_STOP_FAILED = {
    "immediate": ("{} を停止できません",
                  "装置の状態を確認して、もう一度停止してください"),
    "graceful": ("{} に区切りで停止を予約できません",
                 "装置の状態を確認して、もう一度予約してください"),
    "cancel": ("{} の予約を取り消せません",
               "装置の状態を確認して、もう一度取り消してください"),
}
# 2 台以上の開始を、どの装置にも RUN を送る前に拒否するときに付ける文。グループの一部
# だけが動き出していないことを伝える。1 台の開始では付けない(拒否の文が同じことを伝える)
_NONE_STARTED = "まだどの装置も開始していません"
# 予約の取り消しが、応答がなくて送れなかったときの次にできること。取り消しは残さない
# ので、押し直すのは接続が戻ってからである。画面が「予約を取り消す」を押せなくするのは
# 未検出の間だけで、応答がないだけの間は押せ、送れずにこの文が出る
_CANCEL_WHEN_BACK = "接続が戻ってから、もう一度取り消してください"
# 応答がないために止め方を送れず、戻ったときに送るレーン(Coupler._keep_stop)の、最後に
# 付ける文。送れなかったと伝える文と理由の後に、何が起きるかを書く
_STOP_LATER = {
    "immediate": "接続が戻ったら、{} を停止します",
    "graceful": "接続が戻ったら、{} に区切りで停止を予約します",
}


def _stop_call(c, mode: str) -> None:
    """止め方(mode)に合う停止の指示を装置へ送る。

    装置がその場で停止するのは、即時の停止だけである。「区切りで停止」は装置への
    予約で、その取り消しは予約の解除である。
    """
    if mode == "graceful":
        c.stop_at_mark()
    elif mode == "lap":
        # グループの 1 台が完了したときの連動。相手の最終周を内側の
        # ラベルで切らないよう、周の境だけで停止させる
        c.stop_at_mark(binfmt.MARK_LAP)
    elif mode == "cancel":
        c.stop_at_mark(release=True)
    else:
        c.stop()


# 1 台の止まり方と、残りの止め方。止まり方が空なのは、実行の途中で装置が起動し直した
# ときで(電源を入れてから一度も実行していない)、未検出から戻ったレーンに多い。
# 止まり方は分からないが停止したことは確かなので、異常と同じくその場で停止させる
_LINKED_MODE = {
    proto.STOP_REASON_ERROR: "immediate",
    proto.STOP_REASON_STOPPED: "immediate",
    proto.STOP_REASON_BUTTON: "immediate",
    "": "immediate",
    proto.STOP_REASON_AT_MARK: "graceful",
    proto.STOP_REASON_FINISHED: "lap",
}
# 止め方の強い順。同じ周期に 2 本が停止して見えたら、強い方で残りを停止させる
# (_just_stopped)
_MODE_ORDER = ("immediate", "graceful", "lap")
# 人がレーンを指して押した止め方(停止、区切りで停止)で停止したときの止まり方。その
# レーンの記録に止め方があれば、この止まり方で停止してもトリガーにしない
# (Coupler._by_hand)
_HAND_REASONS = frozenset({proto.STOP_REASON_STOPPED, proto.STOP_REASON_AT_MARK})
# レーンの記録に残す、人が押した止め方(Coupler._mark_hand_stop)
_HAND_MODES = ("immediate", "graceful")
# 止まり方を人に見せる文。連動停止のメッセージに含める
_LINKED_WHY = {
    proto.STOP_REASON_ERROR: "装置に異常が発生しました",
    proto.STOP_REASON_STOPPED: "停止しました",
    proto.STOP_REASON_BUTTON: "装置のボタンで停止しました",
    "": "停止しました",
    proto.STOP_REASON_AT_MARK: "区切りで停止しました",
    proto.STOP_REASON_FINISHED: "完了しました",
}


@dataclass
class _Start:
    """1 回の開始の、段階の間で受け渡す値。"""
    plan: list
    prepare: object
    continuing: dict | None
    held: set
    links: list = field(default_factory=list)
    before: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    builds: dict = field(default_factory=dict)
    # 実行中のレーンの図を描く変換結果(装置ごと)
    views: dict = field(default_factory=dict)
    # 開始した時点の定義の id と、その中の手順の名前(装置ごと)
    frozen_of: dict = field(default_factory=dict)
    fws: set = field(default_factory=set)
    # レーンごとの、RUN を送る直前の時刻(開始の記録の時刻にする)
    sent_at: dict = field(default_factory=dict)
    # RUN を受けたか、受けたかもしれない装置の ID。開始が揃わなかったときに、
    # その装置の前の実行の終わりを書く(Coupler._end_rolled_back)
    ran: list = field(default_factory=list)
    # RUN を受けた装置の、装置が返した実行の識別({装置 ID: proto.run_of}。RUN の
    # 応答の後に読んだ状態から。レーンの記録の run にする)
    runs: dict = field(default_factory=dict)
    skew_ms: int | None = None
    # 開始を指示した時刻(UNIX 秒)。None なら待たされたかを見ない(周回を終えるための
    # 続きは、装置が停止したのを見てから監視が始める)
    asked_at: float | None = None


def _other_run(lane: dict, st: dict) -> bool:
    """装置の状態 st の実行が、レーンの実行と違うと分かるか(どちらの実行の識別も
    分かり、違う)。"""
    mine, now = lane.get("run"), proto.run_of(st)
    return bool(mine and now and tuple(mine) != now)


def _same_run(got: tuple, lane: dict) -> bool:
    """DeviceLink.ended_run(停止した実行の (手順の id, 状態, 時刻))が、レーンの実行の
    ものか。手順の id と、分かれば装置が返す実行の識別(レーンの記録の run)で見る。"""
    if got[0] != lane.get("proc_id"):
        return False
    mine, then = lane.get("run"), proto.run_of(got[1])
    return not (mine and then and tuple(mine) != then)


class Coupler:
    """グループの開始・一括操作の停止・連動停止を扱う。"""

    POLL_S = 0.5

    def __init__(self, project, pool):
        self.project = project
        self.pool = pool
        self._lock = threading.Lock()
        # 周回を終えるための続きで、開始の途中のフラグを立てたときの装置の写し
        # ({装置 ID: DeviceLink.reading}。_send_runs で立て、フラグと一緒に消す)。画面の
        # 状態は、フラグの立つ装置にこれを使う(view)
        self._start_readings: dict = {}
        # 周に入るのを待つ連動停止(linked_stop の later)を、装置へ送るときと取り消す
        # ときに取る。送る直前に取り消されて、取り消した予約を送ることがないように
        # する(_send_later、_cancel_later)。self._lock より先に取る
        self._later_lock = threading.Lock()
        # close で立て、監視のスレッドを終わらせる
        self._stop = False
        self.lap_ends = LapEnds(self)
        self._state = self._load_runstate()
        # 合流点の待ち合わせの記録(再起動で失われてよい)
        self._joins = JoinWaiter(project)
        # 開始の RUN を送っている途中のレーン。キーは装置 ID、値はそのレーンを
        # 始めようとしている start_group の数である。開始は RUN を順に送り終えてから
        # グループを作り直すので、それまでレーンは前のグループに属したままである。
        # そのレーンを前のグループの連動停止の対象にしない(_linked_stop)
        self._starting: dict[str, int] = {}
        # 人の止め方(stop_lanes)を送っている途中のレーン。キーは装置 ID、値はその
        # レーンへ送っている stop_lanes の数である。その間は、レーンが停止しても
        # 連動停止のトリガーにしない(_just_stopped)。読み書きは self._lock の下で行う
        self._stopping: dict[str, int] = {}
        # RUN を送ってからグループを記録するまでの、レーンの開始位置。この間も装置は
        # 実行中なので、記録だけを読むと前の実行の開始位置を画面に見せてしまう
        self._start_pending: dict[str, dict] = {}
        # 前の周期でレーンが動いていたか。連動停止は「動いていた」から
        # 「動いていない」への変化だけをトリガーにする
        self._was_busy: dict[str, bool] = {}
        # 全員停止を最初に見た時刻。キーはグループの識別子。1 本の値にすると、
        # グループが 2 つあるときに互いの観測を消し合う。監視と解除(dissolve_group)
        # が読み書きするので、self._lock の下で行う
        self._idle_since: dict[str, float] = {}
        # レーンごとの、いま何周目かと、停止を最初に見た時刻。キーは装置 ID。
        # 装置は周回を数えないので、周の進みは最後に通った区切りと
        # 変換の対応表から出す。グループではなくレーンで持つのは、相手より先に終わる
        # レーンの終わりを、相手を待たずに記録するためである
        self._lane_lap: dict[str, int] = {}
        # 停止を最初に見た時刻は、(単調時計, UNIX 秒)で持つ。確定の待ちは単調時計で
        # 測り、記録の時刻には停止したように見えたときの UNIX 秒を使う(2 つの時計は
        # 基準が違う)
        self._lane_idle_since: dict[str, tuple[float, float]] = {}
        # レーンごとの、最後に書いた周の行の (周, その周の境を通ったときの経過
        # フレーム数, 時刻, 実行していた手順の id)。キーは装置 ID。監視が読む間に
        # 2 周以上進んだとき、抜けた周の境を経過フレーム数から出すのに使う
        # (_note_laps)。_lane_lap と同じく開始のたびに捨てる
        self._lane_lap_head: dict[str, tuple] = {}
        # レーンごとの、開始してからまだ周の行を書いていない実行の (手順の id, 開始の周,
        # 頭の経過フレーム数が分かる周の {周: 経過フレーム数})。キーは装置 ID。監視が
        # 初めて読む前に通り過ぎた周の行を、開始の周から書くのに使う(_note_laps)。開始の
        # ときに置き、周の行を書いたら捨てる。サーバーを起動し直すと消える(そのときは
        # どの周まで書いたか分からないので、前の周を書かない)
        self._lane_lap_from: dict[str, tuple] = {}
        # レーンごとの、いまの RUN を送る直前の時刻(開始の記録の時刻)。周の行の時刻は
        # これより前にしない(_note_laps)
        self._lane_run_at: dict[str, float] = {}
        # 装置が到達しえない周を読んで、周の行を書かなかったことを操作画面のサーバーの
        # 出力に出したレーン。キーは装置 ID。同じ実行では 1 回だけ出す(_skip_laps)。
        # 周回を終えるための続きでない開始のときに捨てる
        self._laps_skipped: set[str] = set()
        # 動いている間に未検出になったレーンの、未検出になる前の状態(RUNNING か
        # AWAITING)。キーは装置 ID。未検出になった時と戻った時を記録に書くのに使う
        # (_note_gone)
        self._lane_gone: dict[str, str] = {}
        # 装置ごとの、監視が前の周期に読んだ、未検出から戻った回数
        # (DeviceLink.back_count)。周期の間に未検出になって戻ったことを、数が増えた
        # ことで知る(_note_gone)。キーは装置 ID
        self._lane_back_count: dict[str, int] = {}
        # 装置ごとの、動いたまま未検出から戻った時の行(PC_BACK)の時刻。その実行の
        # 終わりは、この行より後に並べる(_after_run_start)。キーは装置 ID
        self._lane_back_at: dict[str, float] = {}
        # 装置ごとの、周回の終え方の続きを始めるまでに、周の境(か待ち)で止まってほかの
        # レーンを待った長さ(秒)。続きを始めた後に最初に書く周の行か終わりの行に付け、
        # その行で閉じる周の時間から除く(_log_lap、_note_run_end。画面の lists.js の
        # lapSpan)。続きでない開始のときに捨てる
        self._lane_held: dict[str, float] = {}
        # 動いている間に割り当てを解除して、グループから外したレーンの記録(終わりを
        # 記録していないもの)と、そのグループを始めた時刻。キーは装置 ID。同じ装置を
        # 割り当て直したときに、まだその実行を続けていれば担当し直す
        # (_drop_unregistered_lanes)。読み書きは self._lock の下で行う
        self._parked: dict[str, tuple[dict, float | None]] = {}
        # STATUS と照合済みのレーン。キーは「グループの識別子 + / + 装置 ID」。
        # 起動直後は収集がまだ 1 度も行われておらず、status が空である。その
        # まま読むと、動いているグループを「全員停止」とみなして終了時刻を書いてしまう
        self._restored: set[str] = set()
        # 監視が失敗した直近の理由。同じ理由で記録が溢れないよう
        # 「変わったときだけ」書く
        self._watch_error = ""
        self._thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._thread.start()

    # ---- 保存(動いているグループの記録。runstate.json) ----

    def _runstate_path(self):
        return self.project.root / "runstate.json"

    def _load_runstate(self) -> dict:
        """グループの並びと、開始を拒否されたレーンの理由を読む。

        持つのは {"groups": [...], "start_errors": {装置 ID: 理由}} である。
        start_errors は、装置が開始を拒否したレーンの理由で、装置は待機中のまま
        なので、この「異常停止」はサーバーが持つ。次にそのレーンを開始するまで残す
        (周回の終え方の続きを始められなかった理由と同じ。coupling.md D11)。
        起動し直しても残すので、ここに書く。

        識別子とレーンが読めないグループは捨てる。壊れた 1 件でグループの並びごと
        失うより、その 1 件だけを捨てるほうが、残りのグループの連動停止が続くぶん
        安全である。
        連動停止の記録は、読めなければ既定値にしてグループは残す(どの装置の
        グループかは分かるので、捨てるほうが失うものが大きい)。

        欄の形まで見るのは、後の処理が形を前提に読むからである。例えば
        linked_stop が辞書でなければ _clear_pending が例外で失敗し、そのグループは
        毎周期失敗したまま、連動停止も終わりの記録も行われない。
        """
        try:
            data = json.loads(self._runstate_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        raw = data.get("groups") if isinstance(data, dict) else None
        groups = []
        for g in raw if isinstance(raw, list) else []:
            if not isinstance(g, dict) or not isinstance(g.get("id"), str) \
                    or not g["id"]:
                continue
            lanes = g.get("lanes")
            if not isinstance(lanes, list):
                continue
            if not all(isinstance(x, dict) and isinstance(x.get("dev"), str)
                       and x["dev"] for x in lanes):
                continue
            ls = g.get("linked_stop")
            if ls is not None and not isinstance(ls, dict):
                g["linked_stop"] = None
            elif ls is not None:
                if "replaced" in ls and not (
                        isinstance(ls["replaced"], list)
                        and all(isinstance(x, dict) for x in ls["replaced"])):
                    # 置き換えた前の連動停止の並び(_linked_stop の over)
                    ls.pop("replaced")
                for one in (ls, *(ls.get("replaced") or [])):
                    _drop_bad_fields(one)
            for x in lanes:
                if "wait_end" in x and not _wait_end_ok(x["wait_end"]):
                    # 読めない待つ間の終わりは捨てる(待っていたレーンの終わりは、装置の
                    # 止まり方で書く)
                    x.pop("wait_end")
                if "stop_later" in x and not (isinstance(x["stop_later"], str)
                                              and x["stop_later"] in _STOP_LATER):
                    # 読めない、戻ったら送る止め方は捨てる。そのまま送ると、止め方の
                    # 振り分け(_stop_call)で「停止」になり、誰も押していない停止で
                    # 装置が停止する
                    x.pop("stop_later")
                if "hand_stop" in x and x["hand_stop"] not in _HAND_MODES:
                    # 読めない、人が押した止め方は捨てる(そのレーンが停止すれば、連動
                    # 停止で残りのレーンを停止させる)
                    x.pop("hand_stop")
                if "picks" in x and not _picks_ok(x["picks"]):
                    # 読めない選択の記録は捨てる(レーンの図は、その通過を選んでいない
                    # 形で描く)
                    x.pop("picks")
                if "rows" in x and not (isinstance(x["rows"], list) and all(
                        isinstance(n, str) for n in x["rows"])):
                    # 読めない図の行は捨てる(送った手順のコピーから求め直す)
                    x.pop("rows")
                if "run" in x and x["run"] is not None and not (
                        isinstance(x["run"], list) and len(x["run"]) == 2
                        and all(isinstance(n, int) and not isinstance(n, bool)
                                for n in x["run"])):
                    # 読めない実行の識別は捨てる(手順の id だけで、別の実行かを見る)
                    x.pop("run")
            if "lap_end" in g and not _lap_end_ok(g["lap_end"]):
                # 読めない終え方は捨てる(実行は予約の位置で停止し、普通の止まり方
                # として記録される)
                g.pop("lap_end")
            groups.append(g)
        errs = data.get("start_errors") if isinstance(data, dict) else None
        if not isinstance(errs, dict):
            errs = {}
        # 読めない項目は捨てる(グループと同じく、その 1 件だけ)
        start_errors = {k: v for k, v in errs.items()
                        if isinstance(k, str) and k and isinstance(v, str) and v}
        return {"groups": groups, "start_errors": start_errors}

    def _save_runstate(self) -> None:
        # 一時ファイル経由で置き換える(files.write_file。書き込み途中のクラッシュで
        # 壊れた JSON が残ると、再起動後にグループの記録ごと失われる)
        write_file(self._runstate_path(),
                   json.dumps(self._state, ensure_ascii=False, indent=2))

    # ---- グループの対象候補(登録情報の全リンク) ----

    def members(self) -> list:
        """登録情報の全リンク。グループの実際のレーンと並び順は start_group の plan で
        決まる(全リンクの部分集合でよい)。"""
        return self.pool.links()

    def _link(self, name: str):
        return self.pool.get(name)

    # ---- 装置の指し方(runstate.json は装置 ID、画面・CLI は名前) ----
    #
    # runstate.json は装置を装置 ID で指す(P23: 同一性は名前に
    # 頼らない)。実行中に改名しても監視が対象を検出できなくならない。名前を使うのは
    # 人に見せるところだけで、snapshot() が今の名前に変換して渡す。

    def _by_id(self) -> dict:
        """装置 ID -> 登録情報のリンク。"""
        return {link.registered_id: link for link in self.pool.links()
                if link.registered_id}

    def _names(self) -> dict[str, str]:
        """装置 ID -> いまの表示名。"""
        return {link.registered_id: link.cfg.get("name", "")
                for link in self.pool.links() if link.registered_id}

    def _shown_dev(self, dev_id: str, names: dict[str, str]) -> str:
        """記録の装置 ID を、画面のログの装置の列と同じ呼び名にする(lists.js の
        logDevLabel)。いまの行の名前、その装置を前に割り当てていた行の名前の順で、
        どちらもなければ「装置 ID」と下 4 桁である(登録を削除した装置。装置の行と
        同じ言い方で、下 4 桁だけでは何の数か分からない)。空にすると、
        連動して停止したレーンの前回の実行が、自分で停止したのと同じ文になる。"""
        if not dev_id:
            return ""
        if names.get(dev_id):
            return names[dev_id]
        for link in self.pool.links():
            if dev_id in registry.former_ids(link.cfg):
                return link.cfg.get("name", "")
        return f"装置 ID {dev_id[-4:].upper()}"

    def _id_of(self, name: str) -> str:
        """表示名 -> 装置 ID。分からなければ空(記録には含まれない)。"""
        for link in self.pool.links():
            if link.cfg.get("name") == name:
                return link.registered_id
        return ""

    # ---- 「動いているか」 ----
    #
    # runstate.json には書かない。書くと、サーバーを終了した時点の値が装置の実体と
    # ずれる。link.error と link.gone は見ない。決まりは「未検出間は
    # 最後に読めた値を出す」であり、応答が途切れた瞬間にグループが薄くなる
    # のは誤りだからである

    @staticmethod
    def _lane_busy(link) -> bool:
        """そのレーンが「実行中」か「選択待ち」か。"""
        st = link.status or {}
        return bool(st.get("running") or st.get("awaiting"))

    def _group_active(self, lanes: list, by_id: dict) -> bool:
        """グループのレーンが 1 本でも動いているか。"""
        return any(self._lane_busy(by_id[x["dev"]]) for x in lanes
                   if x.get("dev") in by_id)

    @staticmethod
    def _closed(g: dict) -> bool:
        """1 回の実行の終わりを記録したグループか。連動停止を始めたグループも含める。

        監視は、そのグループの終わりの記録と周回を終える予約を二度は行わない。連動
        停止は続ける。送れなかった対象への送り直しと、停止する周に入るのを待つ対象への
        予約と、後から停止したレーンに連動する、より早い止め方への置き換えである
        (_watch_group)。
        """
        return g.get("ended_at") is not None or bool(g.get("linked_stop"))

    @staticmethod
    def _end_if_settled(g: dict) -> None:
        """連動停止を始めたグループ g の終わりの時刻(ended_at)を、連動停止を対象の
        全員へ送り終えたら書く(self._lock の下で呼ぶ)。送り終えたのは、送り直す対象
        (resend)も、停止する周に入るのを待つ対象(later)も残らなくなったときである。
        止まる先の予約を受けたレーンは、まだ動いていてよい。

        連動停止の記録(linked_stop)を置いた時点で、監視はそのグループを閉じたものとして
        扱う(_closed)ので、この時刻は監視の判断には使わない。グループの状態として
        /api/state に含まれる。
        """
        ls = g.get("linked_stop") or {}
        if not ls.get("resend") and not ls.get("later") and g.get("ended_at") is None:
            g["ended_at"] = _now()

    @staticmethod
    def _kept_stop_covers(lane: dict, mode: str) -> bool:
        """レーンの記録 lane に、送れなかった人の止め方(戻ったら送る stop_later。
        _keep_stop)があり、連動停止の止め方 mode がそれより早くないか。そのときは、
        そのレーンを連動停止の対象にしない(_linked_stop、_resend_linked、_send_later)。

        そのレーンは、戻ったら人の止め方で停止する。対象にすると、同じレーンへの
        「戻ったら送る」が 2 つに分かれ、戻った周期にどちらが先に送られるかで、人が
        押した止め方が連動と記録され、起きなかった連動停止の記録も残る。連動停止の
        ほうが早ければ(「区切りで停止」を残したレーンへの即時の連動停止)、対象に
        する。つながっていれば、区切りの予約を持つレーンにも即時の連動停止が送られる。
        """
        kept = lane.get("stop_later")
        return bool(kept) and _MODE_ORDER.index(mode) >= _MODE_ORDER.index(kept)

    def _own_stop(self, g: dict):
        """連動停止を始めたグループで、トリガーにする止まり方かを返す関数(keep)。

        トリガーにしないのは、自分で完了したレーンと、連動停止で停止させたレーン
        (_linked_cause)である。連動停止で停止させたレーンをトリガーにすると、連動停止が
        連動停止を呼ぶ。自分で完了したレーンは、完了に連動したときの止め方(完了した
        1 台と同じ周回数まで実行し、周回数が同じか少ないレーンは自分で完了させる。
        coupling.md 1 節)のとおりに停止している。人が画面やコマンドでレーンを指して
        押した止め方で停止したレーンもトリガーにしない(_by_hand。_just_stopped が見る)。
        """
        def keep(link, why: str) -> bool:
            if why == proto.STOP_REASON_FINISHED:
                return False
            stopped = (link.status or {}).get("stopped_mask")
            return not self._linked_cause(link.registered_id, why, g,
                                          stopped=stopped)[0]
        return keep

    # ---- 識別子でグループを探す ----

    def _group_by_id(self, gid: str) -> dict | None:
        """識別子でグループを探す(self._lock の下で呼ぶ)。"""
        for g in self._state.get("groups") or []:
            if g.get("id") == gid:
                return g
        return None

    def _stop_targets(self, devs: list | None = None) -> list:
        """停止の操作(stop_lanes)の対象を登録情報順で返す。

        レーンを指すと、指したレーンだけが対象になる。レーンのボタンはそのレーンを、
        グループの見出しはグループの全レーンを、一括操作の行はチェックを付けたレーンを
        指す。グループの一部のレーンだけを停止させてよい。手順の途中で役割を終えたレーンを
        停止させ、残りを進めることがある(coupling.md 1 節)。指さないときは、動いている
        グループのレーンの和である。動いているグループが 1 つもなければ、登録情報の
        リンクのうち、装置が割り当ててあるものをすべて返す。割り当てを解除した行には
        停止させる装置がない。レーンを指す並びが空なら(指したレーンが押した時点で 1 本も
        なかった)、対象はない(stop_lanes は何もしない)。並びがない(None)ときだけが、
        指さない停止である。
        """
        links = self.members()
        if devs is not None:
            want = {self._id_of(d) for d in devs} - {""}
            return [link for link in links if link.registered_id in want]
        by_id = {link.registered_id: link for link in links
                 if link.registered_id}
        return self._active_targets(links, by_id)

    def _group_targets(self, devs: list) -> list:
        """指したレーンと、そのレーンが属するグループの全レーンを登録情報順で返す。

        周回の終え方(lapends)の対象である。終え方はグループの全レーンを同じ周回数で
        終える(合流点の相手と周がずれないようにする。coupling.md D11)。レーンが
        属するグループは 1 つだけである(開始するレーンは前のグループから抜ける)。
        先に完了して単独で始め直した相手を、前のグループのレーンの並びから
        巻き込むことはない。
        """
        links = self.members()
        by_id = {link.registered_id: link for link in links
                 if link.registered_id}
        want = {self._id_of(d) for d in devs} - {""}
        ids = set(want & set(by_id))
        with self._lock:
            for g in self._state.get("groups") or []:
                lanes = {x.get("dev") for x in g.get("lanes") or []}
                if lanes & want:
                    ids.update(lanes & set(by_id))
        return [link for link in links if link.registered_id in ids]

    def _active_targets(self, links: list, by_id: dict) -> list:
        """指定のない停止の対象(_stop_targets)。動いているグループのレーンの和で、
        動いているグループがなければ、装置を割り当てた登録情報のリンクのすべてである。"""
        ids: set[str] = set()
        with self._lock:
            for g in self._state.get("groups") or []:
                lanes = [x for x in g.get("lanes") or []
                         if x.get("dev") in by_id]
                if self._group_active(lanes, by_id):
                    ids.update(x["dev"] for x in lanes)
        if not ids:
            ids = set(by_id)
        return [link for link in links if link.registered_id in ids]

    # ---- グループの開始 ----

    def start_group(self, plan: list[dict], prepare=None,
                    continuing: dict | None = None,
                    asked_at: float | None = None) -> dict:
        """グループのレーンへ転送してから続けて開始する。

        plan は [{dev, name, laps, start}] を 1 件以上である。レーン 1 本の
        開始も、1 台のグループとして同じ経路を通る。レーンと並び順は
        plan で決まり、登録情報の全リンクの部分集合でよい。

        - laps は合計の周回数である。None(または欄がない)は無限を表す。
         0 は使わない
        - start は開始位置で、None(または欄がない)は先頭である。中身は変換器の
          開始位置の一覧の項目そのもの({name, path, block, turns})で、装置には
          渡さず、その位置から続く手順を変換器が合成する
        - dev は装置の名前である。空でよいのは装置が 1 台のときだけで、その
          装置を指す(装置を指さないコマンドのため)。2 台以上あるときに並び順で
          決めると、利用者が選んでいない装置が動く

        - finals は、周回数で当てはまる周が変わる「対象の回」を解決するか
          (dsl.compile_procs)。欄がなければ解決する

        continuing は、実行中に周回数を変えるために停止した実行の続きを始めるときの
        元の実行である({"started_at", "notes", "lanes": {装置 ID: レーンの記録},
        "ended": [先に終えたレーンの装置 ID], "order": [元のレーンの並び],
        "since": {装置 ID: 元の実行の開始の時刻}})。
        利用者には 1 回の実行に見せるので、開始の記録を書かず、停止した実行の終わりも
        記録しない。開始の時刻とメッセージと開始位置は、元の実行のものを持ち越す。
        先に終えたレーンも、終わりを記録したまま続きのグループへ移す。plan の
        項目の laps_done は、周回数のくり返しの後ろから続けるレーンの終えた周の
        数である(変換に周がなくても、周回数と何周を出すため)。

        asked_at は開始を指示した時刻(UNIX 秒)である。装置と通信する前に、そこから
        START_WAIT_S 秒を超えて待たされていたら、拒否する(_too_late)。

        開始できたときは {"ok", "group", "skew_ms", "warnings"} を返す。拒否する
        ときは _reject が作る {"error", "lanes", "field"} である。
        """
        held: set[str] = set()
        try:
            return self._start_group(plan, prepare, continuing, held, asked_at)
        finally:
            with self._lock:
                self._unmark_starting(held)

    def _mark_starting(self, held: set, devs) -> None:
        """開始の RUN を送っている途中のフラグを立てる(self._lock の下で呼ぶ)。

        held は、この開始が付けたフラグである。2 つの start_group が同じ装置を同時に
        始めようとすることがある(装置は後の RUN を拒否する)ので、数で持つ。
        """
        for dev in devs:
            if dev not in held:
                held.add(dev)
                self._starting[dev] = self._starting.get(dev, 0) + 1

    def _drop_start_readings(self, held: set) -> None:
        """続きの開始で控えた装置の写し(_start_readings)を外す。RUN が拒否されたか
        応答を失って、開始が長引くか揃わないときに呼ぶ。外した後の画面は、装置の今の
        状態を出す(RUN を受けたレーンを、続きの前の待つ状態のまま見せ続けない)。"""
        with self._lock:
            for dev in held:
                self._start_readings.pop(dev, None)

    def _unmark_starting(self, held: set) -> None:
        """この開始が付けたフラグをクリアする(self._lock の下で呼ぶ)。

        何度呼んでもよい。
        """
        for dev in held:
            self._start_pending.pop(dev, None)
            self._start_readings.pop(dev, None)
            n = self._starting.get(dev, 0) - 1
            if n > 0:
                self._starting[dev] = n
            else:
                self._starting.pop(dev, None)
        held.clear()

    def _start_group(self, plan: list[dict], prepare, continuing: dict | None,
                     held: set, asked_at: float | None = None) -> dict:
        """start_group の中身。held は、この開始が付けた「RUN を送っている途中」の
        フラグで、グループを作るときに外す(拒否したときと例外のときは start_group が
        外す)。

        段階を順に進め、どれかが拒否の文を返したら、そこで開始しない。
        """
        s = _Start(plan=plan, prepare=prepare, continuing=continuing, held=held,
                   asked_at=asked_at)
        for stage in (self._resolve_lanes, self._check_and_build_lanes,
                      self._check_across_lanes, self._prepare_devices,
                      self._transfer, self._still_held, self._send_runs):
            refused = stage(s)
            if refused:
                if self._stopped_before_any_run(s):
                    return {"error": "", "changed": True}
                self._end_rolled_back(s)
                return refused
        return self._record_group(s)

    def _stopped_before_any_run(self, s: _Start) -> bool:
        """周回を終えるための続き(continuing)を始めている途中で、どの装置にも RUN を
        送る前に、止め方が押されたか。

        押されていたら、続きは中止する(_still_held と同じ {"changed": True} を返す)。
        転送の残りは送らず(_transfer)、押した後に拒否された理由(転送の失敗など)も
        示さない(_start_group)。周回の終え方は、止め方を受けた stop_lanes がもうやめて
        いて、終わりはその止め方で書く(LapEnds._restart_with)。RUN を受けた装置が
        あるときは、開始が揃わなかったときの後始末に任せる(_end_rolled_back)。
        """
        if s.continuing is None or s.ran:
            return False
        return self.lap_ends._restart_cancelled(
            s.continuing.get("gid") or "", set(s.continuing.get("lanes") or {}))

    def _end_rolled_back(self, s: _Start) -> None:
        """開始が揃わず、RUN を受けた装置を後始末で停止させたとき、その装置の前の実行の
        終わりを、始める前の状態で書く(開始の途中のフラグをクリアする前に呼ぶ)。

        開始はグループを作らないので、レーンは前のグループに残る。装置は後始末の区切り
        まで別の手順で動き、停止した後もその実行の止まり方と区切りを返す。前の実行の
        レーンでそれを読むと、終わりの行の周と停止した所がでたらめになる(周回数が無限の
        手順の後なら、到達しえない周を読む。_note_laps の安全策)。

        周回を終えるための続きを始められなかったときは、止まって待っていた所がその
        実行の終わりで、止まり方は「周回の終え方を変更できずに停止」、時刻は続きを
        拒否されたこの時刻である(レーンの記録の wait_end の at。_note_run_end。続きを
        待つ間は 1 回の実行の途中なので、装置が停止した時刻にはしない)。lapends の
        _failed も同じ印を付け、続きの RUN を受けなかった装置の終わりは監視が書く。
        """
        if not s.ran:
            return
        refused = time.time()
        with self._lock:
            if s.continuing is not None:
                for g in self._state.get("groups") or []:
                    for x in g.get("lanes") or []:
                        if x.get("dev") in s.ran and not x.get("ended"):
                            x["wait_end"] = {"at": refused, "failed": True}
            unended = self._take_unended(s, set(s.ran), restarting=False)
            if unended:
                self._save_runstate()
        # レーンは前のグループに残っているので、いまのグループの記録で書き、前回の
        # 結果もレーンに残す(グループのコピーは渡さない)
        for link, x, at, st, _old in unended:
            self._note_run_end(link, x, at=at, status=st)

    def _take_unended(self, s: _Start, ids: set, restarting: bool) -> list[tuple]:
        """ids の装置の、前の実行の終わりをまだ記録していないレーンに終わりの印を付け、
        終わりを書くための (リンク, レーンのコピー, 時刻, 始める前の状態, 前のグループの
        連動停止) の並びを返す(self._lock の下で呼ぶ)。

        停止してから確定(1.5 秒)を待つ前に始め直すと、終わりの記録がないまま次の開始が
        並び、周の記録が 2 回の実行をまたいで 1 行になる。装置が次の実行を始めた後は、
        前の実行の止まり方と位置を装置から読めないので、始める前の状態で書く。
        restarting は、周回を終えるための続きを始めたか。停止した所は終わりではない
        ので、印だけを付ける。
        """
        by_dev = {link.registered_id: link for link in s.links}
        out: list[tuple] = []
        for g in self._state.get("groups") or []:
            for x in g.get("lanes") or []:
                link = by_dev.get(x.get("dev"))
                st = s.before.get(x.get("dev")) or {}
                if (x.get("dev") in ids and not x.get("ended")
                        and link is not None and st
                        and not (st.get("running") or st.get("awaiting"))):
                    x["ended"] = True
                    x.pop("stop_later", None)
                    if restarting:
                        continue    # 続きを始める。停止した所は終わりではない
                    # 監視がまだ停止したのを見ていなければ、この開始の最初の
                    # RUN の時刻にする。前の実行はそれより前に終わっている。
                    # 監視が初めて見たのがその RUN より後(開始の RUN を送って
                    # いる間に監視が動いた)でも、その時刻より後にはしない。
                    # 画面は、終わりを、その時刻より前に始めた実行のものとする
                    # (lists.js の foldRunLines)
                    first = self._lane_idle_since.get(x["dev"])
                    at = min(s.sent_at.values())
                    if first is not None:
                        at = min(at, first[1])
                    # 収集がその実行の停止した時刻を保持していれば、その時刻にする
                    # (監視が書くときと同じ。_end_time)。開始し直す早さで、終わりの行の
                    # 時刻と周の長さが変わらない
                    got = link.ended_run
                    if (got is not None and _same_run(got, x)
                            and (got[1] or {}).get("stop_reason")):
                        at = min(at, self._after_run_start(x["dev"], got[2]))
                    # 前の実行の終わりは、前のグループのレーンの記録と連動停止で
                    # 書く(開始では、書くのはレーンが新しいグループへ移った後になる)
                    out.append((link, dict(x), at, st, {
                        "linked_stop": json.loads(json.dumps(
                            g.get("linked_stop")))}))
        return out

    def _resolve_lanes(self, s: _Start) -> dict | None:
        """計画の装置名を、登録情報の装置に対応させる。"""
        plan = s.plan
        if not plan:
            return _reject("開始するレーンが選択されていません")
        members = self.members()
        if not members:
            return _reject("装置が登録されていません")
        if len(members) >= 2 and not all(p.get("dev") for p in plan):
            return _reject("実行する装置が指定されていません", field="")
        by_name = {link.cfg.get("name"): link for link in members}
        first = members[0].cfg.get("name", "")
        plan = [{**p, "dev": p.get("dev") or first} for p in plan]
        # plan の装置名で登録情報を探す(登録情報順に依存しない)。重複や未登録の
        # 名前は拒否する。登録情報を別のタブで変えたあと古い画面のまま開始すると起こる
        devs = [p["dev"] for p in plan]
        if len(set(devs)) != len(devs) \
                or any(d not in by_name for d in devs):
            return _reject("装置の一覧が変わりました。画面を再読み込みしてください。")
        links = [by_name[d] for d in devs]   # グループのレーン(plan の順)
        # 始める前の状態。前の実行の終わりをまだ記録していないレーンは、装置が
        # 次の実行を始めた後では前の実行の止まり方(stop_reason)と位置が読めない
        before = {link.registered_id: dict(link.status or {}) for link in links}
        # runstate.json はレーンを装置 ID で追う(P23)。装置 ID のない装置(割り当てを
        # 解除した装置)は追えないので開始しない
        no_id = [p["dev"] for p, link in zip(plan, links, strict=True)
                 if not link.registered_id]
        if no_id:
            return _reject(f"{'・'.join(no_id)} は未割り当てです", no_id, field="")
        s.plan, s.links, s.before = plan, links, before
        # 装置と通信する前に見る(待たされた開始では、手順を送ることもしない)
        return self._too_late(s)

    @staticmethod
    def _too_late(s: _Start) -> dict | None:
        """開始を指示してから START_WAIT_S 秒を超えて待たされていたら、拒否の応答を
        返す。

        見るのは装置と通信する前だけである(操作画面のサーバーがほかの要求の終わりを
        待った、画面からの要求を受け取るまでに待たされた)。手順を送っている時間は
        待たされているのではなく進んでいる時間で、大きな手順や台数の多いグループでは
        長くなる(送る途中で装置が消えたら、転送の失敗として拒否する)。
        """
        if s.asked_at is None or time.time() - s.asked_at <= START_WAIT_S:
            return None
        devs = [p["dev"] for p in s.plan]
        return _reject(sentences(
            f"開始を指示してから {START_WAIT_S:g} 秒以上たったため、"
            f"{'・'.join(devs)} を開始できません",
            _NONE_STARTED if len(devs) > 1 else "", "もう一度開始してください"),
            devs, field="")

    def _check_and_build_lanes(self, s: _Start) -> dict | None:
        """レーンごとのチェックと変換。全レーンが揃うまで、どの装置とも通信しない。

        周回を終えるために先に止まり、ほかのレーンを待つレーンは開始しない。1 回の
        実行の途中なので、画面はそのレーンを実行中として出し、開始を押させない
        (coupling.md D11)。コマンドと古い画面からの開始も、実行中のレーンと同じ文で
        拒否する。続きを始めるとき(continuing)は、その待っていたレーンを始める。
        """
        held = ({} if s.continuing is not None
                else self.lap_ends.holds_and_errors()[0])
        for p, link in zip(s.plan, s.links, strict=True):
            if link.registered_id in held:
                return _reject(_not_ready_text(p["dev"], {"running": True}),
                               [p["dev"]], field="")
            refused = self._check_lane(p, link) or self._build_lane(s, p, link)
            if refused:
                return refused
        return None

    def _check_lane(self, p: dict, link) -> dict | None:
        """レーンの装置が開始できる状態か。"""
        st = link.status
        if not st:
            # まだ一度も収集していない(画面を開いた直後など)。読めて
            # いないことを「待機中ではない」とみなすと、開始した覚えのない
            # 実行があるかのような拒否の仕方になる。ここで一度だけ問い合わせる
            try:
                st = link.call(lambda c: c.status())
                link.write_through(status=st)
            except (DeviceError, OSError) as e:
                # OS の英語の理由は出さない。装置が返した理由だけを付ける
                said = e.message if isinstance(e, DeviceError) else ""
                return _reject(sentences(
                    f"{p['dev']} の状態を取得できません", said,
                    "装置の電源とネットワークを確認してから開始してください"),
                    [p["dev"]], field="", failure=failure_of(e))
        if not link.visible:
            # 「見えている」は、応答があり、かつ Switch と USB でつながって
            # いることである。応答だけを見ると、挿さっていない
            # 装置で開始してしまう。状態を読んだ後に見るのは、読む前は
            # USB の欄がまだないからである
            # つながらない理由は、装置の行と同じ文(link.reason)で示す。
            # 修正方法まで含む。応答はあるのに見えていないのは、Switch と
            # USB でつながっていないときで、その相手を指定する
            # (gui-decisions.md)
            if link.error:
                return _reject(
                    sentences(f"{p['dev']} は開始できません", link.reason),
                    [p["dev"]], field="",
                    failure=(failure_of(link.error_exc)
                             if link.error_exc else "unreachable"))
            # 装置が RUN を拒否する文と同じ言い方にする
            return _reject(sentences(
                f"{p['dev']} は Switch に USB で接続されていないため、"
                "開始できません",
                "装置と Switch の接続を確認してから開始してください"),
                [p["dev"]], field="")
        why = (firmware_problem(link.info.fw_version)
               if link.info is not None else "")
        if why:
            # この padcue で使えないファームウェア(client.FIRMWARE_MIN)。
            # 手順データや命令の形が合わないので、始めない。装置の行と同じ文
            return _reject(sentences(f"{p['dev']} は開始できません", why),
                           [p["dev"]], field="")
        if st.get("usb_suspended"):
            # Switch がスリープしている。入力は本体に送られないので、始めても
            # 何もしないまま完了になる(装置も拒否する。comm-protocol.md の RUN)
            return _reject(sentences(
                f"{p['dev']} は Switch がスリープ中のため、開始できません",
                "Switch のスリープを解除してから開始してください"),
                [p["dev"]], field="")
        # 異常停止は待機と同じ操作性である。ここでは拒否せず、
        # すべてのチェックを通ってから異常を消す
        if st.get("running") or st.get("awaiting") \
                or st.get("state") not in ("IDLE", "ERROR"):
            return _reject(_not_ready_text(p["dev"], st), [p["dev"]], field="")
        return None

    def _build_lane(self, s: _Start, p: dict, link) -> dict | None:
        """レーンの定義を保存し、その保存物から変換する。"""
        plan, continuing = s.plan, s.continuing
        builds, views, frozen_of = s.builds, s.views, s.frozen_of
        warnings, fws = s.warnings, s.fws
        try:
            laps = _laps_of(p)
        except ValueError as e:
            # 周回数の欄は画面が自分でチェックしていて(下限を割ると赤くする)、
            # その欄を上書きすると毎秒の更新で消える。ここは文だけで示す
            return _reject(f"{p['dev']} の周回数: {e}")
        start = _start_of(p)
        # 定義を先に保存し、変換と判定はその保存物だけを読む。続きは元の実行の
        # 保存物から変換する(coupling.md D12)
        before_art = self._continued_art(continuing, link)
        sid, source_name = ((before_art.sources, before_art.source_name)
                            if before_art is not None and before_art.sources
                            else ("", p["name"]))
        if not sid:
            try:
                sid = self.project.sent.freeze(p["name"])
            except OSError:
                # 画面には OS の理由を出さないが、操作画面のサーバーの出力には残す。
                # 残さないと、たまにしか起きない失敗の原因を後から追えない
                traceback.print_exc()
                return _reject(f"{p['dev']} の手順「{p['name']}」を保存できない"
                               "ため、開始できません", [p["dev"]])
        frozen_of[p["dev"]] = (sid, source_name)
        frozen = self.project.sent.frozen(sid)
        finals = p.get("finals", True)

        def make(laps_, start_, drawn_laps=None):
            # 合流点が選択待ちになるのは 2 本以上のグループだけである。周回の終え方の
            # 続きは、終了予定とメッセージを求めたときと同じく変換する(lapends の
            # build。元の実行と同じレーンの数で)
            if before_art is not None and before_art.sources:
                return self.lap_ends.build(before_art, laps_, start_, finals,
                                           name=p["name"], drawn_laps=drawn_laps)
            return frozen.build_safe(source_name, laps_, start_, len(plan),
                                     finals=finals, write=False,
                                     drawn_laps=drawn_laps)
        r, err = make(laps, start)
        # 停止した位置は、停止したときと同じくり返しの回を指すときだけ始める。
        # 画面は毎秒の確認で「見つかりません」に替えるが、手順を保存した
        # 直後に押されると、その確認より先にここへ来る
        at_loops = p.get("start") or {}
        if r is not None and start is not None \
                and not frozen.start_loops_match(source_name, at_loops):
            r = None
        if r is None:
            # 修正する欄は、外して変換し直して決める。画面はその欄を異常の見た目にする
            def passes(laps_, start_) -> bool:
                return make(laps_, start_)[0] is not None
            if start is not None and passes(laps, None):
                # 同じ周回数で、開始位置を外せば変換できるので、原因は開始位置である。
                # 指していたラベルが消えた形が典型である。周回の終え方の続きの位置は、
                # 利用者が選んだものではない(停止した所から作る)ので、続きを始める
                # 位置と書く
                at = str(p.get("start", {}).get("name") or "")
                where = (f"「{at}」" if at else "続きを始める位置"
                         if continuing is not None else "選択した開始位置")
                return _reject(f"{p['dev']} は{where}から開始できません。"
                               "その位置は手順にありません。"
                               "別の開始位置を選択してください。",
                               [p["dev"]], field="start")
            if isinstance(laps, int) and passes(None, None):
                # 周回数を決めなければ変換できるので、原因は周回数である。周回数
                # ぶんの総フレーム数が上限を超える形が典型である(画面の欄は上限を
                # 持たない)。総フレーム数の上限を示しても、周回数をいくつに
                # すればよいかは分からないので、指定できる周回数の上限を示す
                most = _most_laps(lambda n: passes(n, start), laps,
                                  start.turns[0] if start and start.turns else 1)
                return _reject(sentences(
                    "周回数が大きすぎます",
                    f"この手順では {most} 周まで指定できます" if most else ""),
                    [p["dev"]], field="laps")
            return _reject(f"{p['dev']} の手順「{p['name']}」: {err}",
                           [p["dev"]])
        # 開始の後に改名された手順の続きは、今の名前で記録する
        builds[p["dev"]] = replace(r, name=p["name"])
        # 実行中のレーンの図は、送る手順と同じ保存物から描く(開始した後にファイルが
        # 編集されても、図は装置が実際に入力するものと一致する)。
        # - 台数も送る手順と合わせる。1 台ぶんで変換すると、グループで待つ合流点が
        #   待たずに通る所として省かれ、図に待ちの枠が出ない
        # - 図は 1 周だけ描き、「最後の回」と「残り n 回」はこの実行の周回数の 1 周目
        #   として解決する(周回数 1 として解決すると、周回数 3 の実行の 1 周目が
        #   最後の回の選択肢で描かれる)
        # - 周回の終え方の続きは新しい周回数で描く(「最後の回を飛ばして」の続きは、
        #   最後の回のない周)
        views[p["dev"]] = make(None, None, drawn_laps=laps)[0]
        # 変換の警告は、開始の時点で初めて出るものがある。
        # 周回数が決まらないと、「5 回目」が周回数を超えることも、無限の
        # くり返しに「最後の回」がないことも判定できない。手順の一覧と
        # 編集画面は周回数を決めずに変換するので(dsl.compile_procs の
        # laps が None)、そこには出ない
        warnings += [_build_warning(p, w) for w in r.warnings]
        info = link.info
        if info is not None:
            fws.add(info.fw_version)
        return None

    def _check_across_lanes(self, s: _Start) -> dict | None:
        """レーンどうしの照合。"""
        plan, links, builds, warnings = s.plan, s.links, s.builds, s.warnings
        fws, frozen_of = s.fws, s.frozen_of
        if len(fws) > 1:
            warnings.append(_note(
                "ファームウェアのバージョンが一致していません("
                + "・".join(sorted(fws)) + ")。動作が異なる原因に"
                "なるため、同じバージョンにすることをおすすめします。"))
        # 本体識別子が取れていて重複 = 複数の装置が同じ Switch に挿さって
        # いる。1 台の本体で複数コントローラーという使い方もあり得るので
        # 開始を拒否はしないが、挿し間違いの典型なので知らせる
        his = [getattr(link, "console_id", "") for link in links]
        seen: set = set()
        dup = False
        for h in his:
            if h and h in seen:
                dup = True
            if h:
                seen.add(h)
        if dup:
            warnings.append(_note(
                "同じ Switch 本体に複数の装置が接続されています。"
                "意図した構成でなければ、装置を挿している本体を確認して"
                "ください。"))
        # 手順どうしの照合。相手のいない合流点で静かに時刻が
        # ずれるのを、開始の前に防ぐ
        bad, notes = _check_joins(plan, builds)
        if bad:
            return bad
        warnings += [_note(m) for m in notes]
        # 条件と映像ソースの参照先(file-format.md §8-2)
        warnings += self._source_notes(plan, links, builds, frozen_of)
        # 開始位置の周がレーンで違うことは、知らせるだけで拒否しない
        warnings += [_note(m) for m in _check_start_laps(plan, builds)]
        return None

    def _prepare_devices(self, s: _Start) -> dict | None:
        """装置と通信する前の準備と、前の開始のフラグと異常の解除。"""
        plan, links, prepare = s.plan, s.links, s.prepare
        # ここから装置と通信する。prepare は、装置と通信する前に呼び出し元が
        # 済ませておくこと(操作画面のサーバーは手動操作を終える)。拒否する文を返したら
        # 開始しない。チェックより後に呼ぶ(拒否する開始で、相手の手動操作を終えて
        # しまわない)
        if prepare is not None:
            err = prepare(plan, links)
            if err:
                return _reject(err, field="")
        # 前の開始で拒否されたフラグと、周回の終え方の続きを始められなかった理由
        # (レーンの記録にある)は、この開始でクリアされる
        with self._lock:
            ids = {link.registered_id for link in links}
            errs = self._state["start_errors"]
            cleared = bool(ids & errs.keys())
            for i in ids:
                errs.pop(i, None)
            for g in self._state.get("groups") or []:
                for x in g.get("lanes") or []:
                    if x.get("dev") in ids and "lap_end_error" in x:
                        del x["lap_end_error"]
                        cleared = True
            if cleared:
                self._save_runstate()
        # 異常停止は開始がクリアする。拒否するより後にクリアするのは、開始しな
        # かったのに異常のフラグだけがクリアされるのを避けるためである
        for link in links:
            if (link.status or {}).get("state") != "ERROR":
                continue
            try:
                link.write_through(status=link.call(
                    lambda c: (c.clear_error(), c.status())[1]))
            except (DeviceError, OSError):
                continue      # 消せなければ、この後の RUN が拒否されて分かる
            # 解除で装置が書いた状態の行(「異常停止 → 待機中」)を、開始の行より先に
            # 書く。次の収集を待つと、受け取った時刻が開始の行(RUN を送る直前の
            # 時刻)より後になり、開始した後に待機中へ戻ったように読める
            link.read_logs_now()
        # 前の実行の終わりをまだ記録していないレーン(停止してすぐ開始し直した)は、
        # その実行の装置の終わりの行(経過フレーム数を持つ)をいま読んで書く。前の実行の
        # 終わりはこの開始の中で書く(_record_group)。次の収集を待つと、終わりの行は
        # 経過フレーム数なしで出て、装置の行を受け取った後(約 1 秒後)に書き替わる
        with self._lock:
            unended = {x.get("dev") for g in self._state.get("groups") or []
                       for x in g.get("lanes") or [] if not x.get("ended")}
        for link in links:
            if link.registered_id in unended and not self._lane_busy(link):
                link.read_logs_now()
        return None

    def _transfer(self, s: _Start) -> dict | None:
        """転送。全装置への転送が済むまで、どの装置も開始しない。"""
        plan, links, builds = s.plan, s.links, s.builds
        views, frozen_of = s.views, s.frozen_of
        # あわせて、これから実行する手順だけを装置に残す。周回数や開始位置が違えば
        # 別の id になるので、溜めると手順の一覧が同じ手順の別の id で埋まる
        for p, link in zip(plan, links, strict=True):
            if self._stopped_before_any_run(s):
                # 周回を終えるための続きの転送の間に、止め方が押された。残りの装置へは
                # 送らない。人が停止させた直後にその装置を開始し直していると、この
                # 転送が、その開始の送った手順を装置から消してしまい、その開始の RUN が
                # 拒否される(装置に残すのは最後に送った 1 件)
                return {"error": "", "changed": True}
            r = builds[p["dev"]]
            # 装置が報告するのはこの id である。手順のファイルは編集で変わり、
            # 変換し直しても同じ id は作れないので、送る前に送った手順のコピーへ残す
            # (記録の手順名と、実行中の判定の出所になる)
            sid, source_name = frozen_of[p["dev"]]
            self.project.sent.remember(r, views.get(p["dev"]), link.registered_id,
                                        sources=sid, source_name=source_name,
                                        video=link.cfg.get("video"))

            def _push(c, r=r):
                ids = {e["id"] for e in c.list()}
                if r.hash not in ids:
                    c.put(r.blob)
                    c.commit(r.hash)   # 保存キーは内容の id
                    ids.add(r.hash)
                return keep_only(c, ids, {r.hash})
            try:
                link.write_through(listing=link.call(_push))
            except (DeviceError, OSError) as e:
                # 転送は開始より前の段階。まだ誰も実行していないので
                # 巻き戻し(_rollback_started)は不要
                return _reject(sentences(
                    f"{p['dev']} への転送に失敗しました",
                    failure_text(e, link.cfg.get("host") or ""),
                    _NONE_STARTED if len(plan) > 1 else ""), [p["dev"]], field="",
                    failure=failure_of(e))
        return None

    def _still_held(self, s: _Start) -> dict | None:
        """周回を終えるための続き(continuing)を始める直前に、続きを実行するレーンの
        装置を読み直す。転送の間に止め方が押されていたら(中止)、装置のボタンが
        押されていたら(または予約で停止した状態でなくなっていたら)、始めずに
        {"changed": True} を返す。RUN は押されたことを消すので、送った後では分から
        ない。中止なら LapEnds が押した止め方で終わりを書き、ボタンなら次の周期の
        監視が、そのレーンを原因として待つ間を終える(LapEnds._restart_with)。
        後ろに何もなく完了と書いたレーンは、もう待っていないので読まない。
        """
        if s.continuing is None:
            return None
        devs = {link.registered_id for link in s.links}
        if (not self.lap_ends._restart_cancelled(s.continuing.get("gid") or "", devs)
                and self.lap_ends.still_held(s.links)):
            return None
        return {"error": "", "changed": True}

    def _send_runs(self, s: _Start) -> dict | None:
        """開始。計画の順に RUN を送る。応答を失ったら、STATUS で確かめてから決める。"""
        plan, links, builds, held = s.plan, s.links, s.builds, s.held
        if s.continuing is not None and not self.lap_ends.begin_runs(
                s.continuing.get("gid") or "",
                [link.registered_id for link in links]):
            # 周回を終えるための続きで、読み直した後に止め方が押された。RUN を送らずに
            # 中止する(_still_held と同じ)。ここを過ぎた後の止め方は、続きが始まった
            # 後の止め方である
            return {"error": "", "changed": True}
        # 最初の RUN より前に、開始の途中のフラグを立てる。周回を終えるための続きでは、
        # このときの装置の写しを控える。画面は、フラグが消えるまで(グループの記録を
        # 置き換えるまで)、続きの前の状態を見る(view)
        with self._lock:
            self._mark_starting(held, [link.registered_id for link in links])
            if s.continuing is not None:
                for link in links:
                    self._start_readings[link.registered_id] = link.reading()
            for p, link in zip(plan, links, strict=True):
                was = (s.continuing or {}).get("lanes", {}).get(link.registered_id)
                self._start_pending[link.registered_id] = (
                    dict(was.get("start_at") or {"kind": "head"}) if was is not None
                    else _start_record(p, builds[p["dev"]].marks))
        started: list[str] = []
        t_done: list[float] = []
        # レーンごとの、RUN を送る直前の時刻(開始の記録の時刻にする)。開始が
        # 揃わなかったときも、前の実行の終わりの時刻に使う(_end_rolled_back)
        sent_at = s.sent_at
        for p, link in zip(plan, links, strict=True):
            r = builds[p["dev"]]
            sent_at[p["dev"]] = time.time()
            try:
                # 周回数は手順の変換で SETCNT に入る。実行では渡さない
                st = link.call(lambda c, r=r: (c.run(r.hash), c.status())[1])
                link.write_through(status=st)
                s.runs[link.registered_id] = proto.run_of(st)
                t_done.append(time.monotonic())
                started.append(p["dev"])
                s.ran.append(link.registered_id)
            except DeviceError as e:
                self._drop_start_readings(held)
                # 装置が明確に拒否した(BUSY 等)。応答喪失と違い、実行中
                # だとしてもそれは別の要求の実行なので取り込まない
                # (STATUS で確認すると先につないでいた側の実行を
                # 自分のグループの実行と誤認する)。装置は待機中のままなので、
                # 「異常停止」はサーバーが持つ(次の開始で消える)
                why = failure_text(e, link.cfg.get("host") or "")
                self._note_start_error(link, why)
                failed = self._rollback_started(started)
                return _reject(sentences(
                    f"{p['dev']} を開始できません", why,
                    *_rollback_text(started, failed)),
                    [p["dev"]], field="", failure="refused")
            except OSError as e:
                self._drop_start_readings(held)
                # RUN は再送できない(非冪等)。実際に実行されたかを STATUS で
                # 確定する。1 回で諦めず数回試す(確認まで失敗したときに
                # 「実行していない」と断定すると、実は実行している装置が監視の
                # 外で動き続ける)
                really = None
                for _ in range(3):
                    try:
                        st = link.call(lambda c: c.status())
                        link.write_through(status=st)
                        s.runs[link.registered_id] = proto.run_of(st)
                        really = bool(st.get("running") or st.get("awaiting"))
                        break
                    except Exception:   # noqa: BLE001  読めなければ少し待って読み直す
                        time.sleep(0.5)
                if really:
                    t_done.append(time.monotonic())
                    started.append(p["dev"])
                    s.ran.append(link.registered_id)
                    continue
                if really is None:
                    # 確認しきれない = 不確定。実行しているかもしれない装置を
                    # 放置しないよう、全員に次の区切りでの停止を予約してから
                    # 知らせる(「一部だけ開始」と同じ止め方。coupling.md D8)
                    # 予約を送れなかったと伝えるのは、開始したと分かっている
                    # レーンだけである(確かめられなかったレーンは、そもそも
                    # 動いていないかもしれない)
                    failed = [d for d in self._rollback_started([*started, p["dev"]])
                              if d in started]
                    s.ran.append(link.registered_id)
                    return _reject(sentences(
                        f"{p['dev']} の開始結果を確認できません",
                        f"{'・'.join(failed)} に区切りで停止を予約できません。"
                        "停止してください"
                        if failed else "開始した装置は次の区切りで停止します",
                        "状態を確認してからやり直してください"),
                        [p["dev"]], field="", failure="unreachable")
                # 「一部だけ開始」が確定した。実行した装置を次の区切りで停止させ、
                # そのことを報告する
                failed = self._rollback_started(started)
                return _reject(sentences(
                    f"{p['dev']} の開始に失敗しました",
                    failure_text(e, link.cfg.get("host") or ""),
                    *_rollback_text(started, failed)),
                    [p["dev"]], field="", failure="unreachable")
        # skew は各装置の開始時刻の広がり(max - min)。開始は順に連発するので
        # 時刻は昇順で、先頭と末尾の差になる
        s.skew_ms = round((max(t_done) - min(t_done)) * 1000) \
            if len(t_done) >= 2 else None
        return None

    def _record_group(self, s: _Start) -> dict:
        """開始したレーンをグループとして記録する。"""
        plan, links, builds, warnings = s.plan, s.links, s.builds, s.warnings
        continuing, held = s.continuing, s.held
        frozen_of, sent_at, skew_ms = s.frozen_of, s.sent_at, s.skew_ms
        # レーンは装置 ID で、plan と同じ並びで持つ(P23)
        lanes = []
        for p, link in zip(plan, links, strict=True):
            laps = _laps_of(p)
            was = (continuing or {}).get("lanes", {}).get(link.registered_id)
            lanes.append({
                # 開始の時点で分かった、このレーンのメッセージ。画面は毎秒の状態から
                # 描く(タブを行き来しても、読み込み直しても、実行中は残る)。
                # 周回を終えるための続きは、元の開始のメッセージを持ち越す
                # (_continued_notes)
                "notes": (self._continued_notes(
                              p, was, self._continued_art(continuing, link), laps)
                          if was is not None else
                          [_lane_note(w) for w in warnings
                           if w.get("dev") == p["dev"]]),
                "dev": link.registered_id,
                "proc": p["name"],
                # 変換の成果物の id。名前は改名されうるので、同一性はこちらで
                # 見る(装置の STATUS の proc と照合できる)
                "proc_id": builds[p["dev"]].hash,
                # 装置の実行の識別(proto.run_of。RUN の応答の後に読んだ状態から。
                # キャッシュの状態は、古い書き戻しで前の実行のものになりうる)。同じ
                # 手順を別の道具が始め直したことを見分ける(_ran_elsewhere)
                "run": list(s.runs.get(link.registered_id) or []) or None,
                # 周回数は「合計」。無限は None である。0 は使わない
                "laps": None if laps == "infinite" else laps,
                # 周回数のくり返しより後ろから始めた実行には、周がない(変換に
                # 周回数のくり返しが入らない)。開始と終わりの記録と進捗は、周を示さない
                "nolaps": not builds[p["dev"]].lap_map,
                # 開始位置。ブロックの識別子が同一性で、名前は見せるためのもの
                # である。装置には渡らない
                "start_at": (dict(was.get("start_at") or {"kind": "head"})
                             if was is not None
                             else _start_record(p, builds[p["dev"]].marks)),
            })
            if p.get("laps_done") is not None:
                lanes[-1]["nolaps"] = False
                lanes[-1]["laps_done"] = p["laps_done"]
            if was is not None and "orig_laps" in was \
                    and was["orig_laps"] != lanes[-1]["laps"]:
                # 周回数を変えた実行。戻す先を持ち越す(戻した実行には持たない)
                lanes[-1]["orig_laps"] = was["orig_laps"]
            # レーンの図の行(その実行で通りうる経路が使う入力の全部)。送るレコードと、
            # 1 周目の図の変換結果から出す(送る手順が開始位置から始まるとき、その手前も
            # 図には描く)。周回数が無限の実行は、周回の終え方で通りうる選択肢の入力も
            # 入れる(positions.ending_builds。行のためだけに、開始のときに変換する)。
            # 周回の終え方の続きは同じ 1 回の実行なので、元の実行の行に足す
            # (続きのレコードだけから出すと、実行の途中で図の行が減る)。今の周に
            # 選んだ選択肢の記録(_keep_picks)も、続きへ持ち越す
            r = builds[p["dev"]]
            sid, source_name = frozen_of[p["dev"]]
            endings = positions.ending_builds(self.project.sent.frozen(sid),
                                              source_name, r.laps, r.lanes)
            lanes[-1]["rows"] = timeline.merge_rows(
                (was or {}).get("rows") or [],
                timeline.rows_of_builds(r, s.views.get(p["dev"]), *endings))
            if was is not None and was.get("picks"):
                lanes[-1]["picks"] = [dict(x) for x in was["picks"]]
        started_at = (continuing or {}).get("started_at") or _now()
        with self._lock:
            groups = self._state.setdefault("groups", [])
            # レーンは最後に開始されたグループにだけ属する。今から開始する
            # レーンは前のグループから抜ける。残りのレーンは前のグループに残る。
            # 消すと、残りのレーンの前回の結果が消え、実行中ならその周と
            # 終わりも記録されなくなる(記録はグループのレーンを見て付ける)
            new_ids = {x["dev"] for x in lanes}
            # 割り当てを解除して外したレーンの装置を、割り当て直して開始した。前の
            # 実行はもう担当し直さない(_take_back_parked)
            for dev in new_ids & set(self._parked):
                del self._parked[dev]
                self._forget_lost(dev)
            ended_ids = set((continuing or {}).get("ended") or [])
            if continuing is not None:
                # 続きを始めている間に押された止め方(元のグループの終え方の記録の late。
                # LapEnds.stop_of)を、続きのレーンの記録に置き、続きを始めた後にその
                # レーンを停止させるために渡す(LapEnds._restart_with)。そのレーンが
                # 停止しても連動停止のトリガーにしない。保存した止め方は元のグループの
                # 終え方の記録にあるので、終え方がなくなれば一緒になくなり、別の
                # グループの続きには使わない
                src_le = next((g.get("lap_end") or {} for g in groups
                               if g.get("id") == continuing.get("gid")), {})
                late = dict(src_le.get("late") or {})
                continuing["late"] = late
                for x in lanes:
                    if late.get(x["dev"]):
                        x["hand_stop"] = late[x["dev"]]
            # 先に終えたレーンの記録は、続きの元のグループからだけ引き継ぐ。続きを始めて
            # いる間に開始し直したレーンは、別のグループの別の実行である(引き継ぐと、
            # その実行と続きが連動停止を送り合う)
            src = (continuing or {}).get("gid")
            # 待つ間にレーンの止め方で終え方から外したレーン(lapends の _leave)も、
            # 終わりを書く前でも移す(押した止め方で停止する途中でも、グループは分けない)
            left_ids = set((continuing or {}).get("left") or [])
            moved = [dict(x) for g in groups if g.get("id") == src
                     for x in g.get("lanes") or []
                     if (x.get("dev") in ended_ids and x.get("ended"))
                     or x.get("dev") in left_ids]
            moved_ids = {x["dev"] for x in moved}
            # 前の実行の終わりをまだ記録していないレーン。ここで印を付け、レーンが
            # 新しいグループへ移った後に書く
            unended = self._take_unended(s, new_ids,
                                         restarting=continuing is not None)
            # 使用済みの識別子は消す前に取っておく。消した後の並びから取ると、
            # 同じミリ秒に開始し直したときに、いま消したグループと同じ識別子ができる
            taken = {g.get("id") for g in groups}
            for g in groups:
                leaving = new_ids | (moved_ids if g.get("id") == src else set())
                g["lanes"] = [x for x in g.get("lanes") or []
                              if x.get("dev") not in leaving]
            groups[:] = [g for g in groups if g["lanes"]]
            gid = _new_group_id(started_at, taken)
            order = (continuing or {}).get("order") or []
            members_of = sorted(
                lanes + moved, key=lambda x: (order.index(x["dev"])
                                              if x["dev"] in order else len(order)))
            groups.append({"id": gid, "started_at": started_at,
                           "ended_at": None, "lanes": members_of,
                           "linked_stop": None,
                           # グループ全体へのメッセージ(すべてのレーンを挙げる文)。
                           # 続きは元の開始のメッセージのままにする(レーンの
                           # メッセージと
                           # 同じ理由)
                           "notes": list((continuing or {}).get("notes") or [])
                           if continuing is not None else
                           list(dict.fromkeys(w["msg"] for w in warnings
                                              if not w.get("dev")))})
            # 開始した時点で「動いていた」ことにしておく。監視が最初に見るより
            # 早く停止させられると、変化を見逃して連動停止が出ない
            # (人が開始直後に停止させるのは普通に起きる)
            for x in lanes:
                self._was_busy[x["dev"]] = True
            # 先に終えたレーンは、停止への変化を連動停止のトリガーにしない。終え方から
            # 外したレーンで、まだ停止する途中のものは、ほかのレーンと同じく変化を見る
            # (押した止め方で停止したならトリガーにしない。_by_hand)
            for x in moved:
                if x.get("ended") or x["dev"] not in left_ids:
                    self._was_busy[x["dev"]] = False
            # 続きは、開始の時刻を元の実行のものにする(1 回の実行に見せる)。記録を
            # 置き換えるのと同じロックの中で行い、画面が続きの開始の時刻を見ないように
            # する
            since = (continuing or {}).get("since") or {}
            for link in links:
                if since.get(link.registered_id):
                    link.carry_run_start(since[link.registered_id])
            # レーンは前のグループを抜けた。前のグループに含まれるのにフラグがない瞬間を
            # 作らないよう、移すのと同じロックの中でフラグをクリアする
            self._unmark_starting(held)
            self._save_runstate()
            on_record = [(x.get("proc_id") or "", x.get("dev") or "")
                         for g in groups for x in g.get("lanes") or []]
        # 動いている実行が使わない保存物を片付ける
        keep = {sid for sid, _name in frozen_of.values()}
        for proc_id, dev in on_record:
            art = self.project.sent.built(proc_id, dev)
            if art is not None:
                keep.add(art.sources)
        self.project.sent.drop_unused(keep)
        for link, x, at, st, old in unended:
            self._note_run_end(link, x, at=at, status=st, group=old)
        for p, x in zip(plan, lanes, strict=True):
            if continuing is None:
                # 続きは同じ実行なので、いまの周をもう一度は記録しない
                self._lane_lap.pop(x["dev"], None)
                self._lane_lap_head.pop(x["dev"], None)
                self._laps_skipped.discard(x["dev"])
                self._lane_held.pop(x["dev"], None)
                r = builds[p["dev"]]
                self._lane_lap_from[x["dev"]] = (r.hash, r.start_lap, _lap_heads(r))
            else:
                # 続きの経過フレーム数は、元の実行の変換の区切りとはつながらない
                self._lane_lap_from.pop(x["dev"], None)
                # 周の境(か待ち)で止まってから、続きの RUN を送るまで待った長さ
                held_from = (continuing.get("held_from") or {}).get(x["dev"])
                if held_from:
                    self._lane_held[x["dev"]] = max(0.0, sent_at[p["dev"]] - held_from)
            self._lane_idle_since.pop(x["dev"], None)
            self._lane_run_at[x["dev"]] = sent_at[p["dev"]]
        for p, link, x in zip(plan, links, lanes, strict=True):
            if continuing is not None:
                break           # 開始の記録は、元の実行のものが 1 つあればよい
            self._log(link, "PC_SET_START",
                      name=p["name"],
                      laps=x["laps"],
                      **({"nolaps": True} if x["nolaps"] else {}),
                      # 欄の名前を at にすると、行そのものの時刻(at)と
                      # ぶつかって時刻が表示に出る
                      start=(x["start_at"].get("name") or ""),
                      # 開始のずれは、2 台以上のグループの最後のレーンの行にだけ
                      # 持つ(測ったのは全体の広がりで、1 台ごとの値ではない)。
                      # 0ms も測った値なので残す。1 台の開始は測っていないので持たない
                      **({"skew_ms": skew_ms}
                         if skew_ms is not None and p is plan[-1] else {}),
                      # 時刻は、その装置へ RUN を送る直前の時刻にする。装置の開始の
                      # 記録は必ずその後に受け取るので、画面は 2 つを組める
                      # (lists.js の foldRunLines)
                      at=sent_at[p["dev"]])
        return {"ok": True, "group": gid, "skew_ms": skew_ms,
                "warnings": warnings}

    def _continued_art(self, continuing: dict | None, link):
        """続きを始めるレーンの、元の実行の成果物。続きでなければ None。"""
        x = ((continuing or {}).get("lanes") or {}).get(link.registered_id)
        if x is None:
            return None
        return self.project.sent.built(x.get("proc_id") or "", link.registered_id)

    def _continued_notes(self, p: dict, was: dict, art, laps) -> list[dict]:
        """周回を終えるための続きのレーンのメッセージ。元の開始のメッセージを持ち越す。

        続きは同じ実行の残りで、その変換は停止した位置より前のブロックを通らない。
        続きの変換のメッセージに入れ替えると、開始のときに出した前のブロックの警告が
        実行の途中で消える。ただし周回数を変えたときは、終え方を選んだ時点から画面に
        出している警告(notes_at_laps)と同じものにする。替えられないとき(変換
        できない)は持ち越す。art は元の実行の送った手順のコピー、laps は新しい周回数
        (_laps_of の形)である。
        """
        got = (self.notes_at_laps(was, art, laps, p.get("finals", True))
               if art is not None else None)
        return list(was.get("notes") or []) if got is None else got

    def notes_at_laps(self, x: dict, art, laps, finals: bool) -> list[dict] | None:
        """レーンの記録 x の開始のメッセージを、元の開始位置から周回数 laps(_laps_of の
        形)と finals(「最後の回」を解決するか)で開始したときのものにする。
        求められなければ None を返す。

        x のメッセージは、x の実行(送った手順のコピー art)の周回数と finals で開始した
        ときのもので、そのうち変換の警告だけを、新しい予定で変換したときの警告に替える
        (無限のくり返しに最後の回がないという警告は、周回数が決まれば当てはまらない)。
        映像ソースなどのほかのメッセージは残す。x のメッセージそのものは書き換えない
        ので、選んだ時点(lapends)と続きの開始で 2 度求めても、同じ警告が重ならない。
        変換は続きと同じく行う(lapends の build。開始した時点の定義から、元の実行と
        同じレーンの数で)。
        """
        notes = list(x.get("notes") or [])
        old = "infinite" if x.get("laps") is None else x["laps"]
        if (old, art.finals) == (laps, finals):
            return notes
        if not art.sources:
            return None
        start_at = x.get("start_at") or {}
        start = (_start_of({"start": start_at})
                 if start_at.get("kind") not in (None, "head") else None)
        name = art.source_name or x.get("proc") or ""
        # レーンの記録のメッセージは、装置と手順の名前を持たない(_lane_note)
        p = {"dev": "", "name": name}

        def warned(n, fin):
            r = self.lap_ends.build(art, n, start, fin, name=name)[0]
            return (None if r is None
                    else [_lane_note(_build_warning(p, w)) for w in r.warnings])

        def key(n):
            return (n.get("line"), n.get("msg"), n.get("block"),
                    tuple(n.get("path") or []))
        before, after = warned(old, art.finals), warned(laps, finals)
        if before is None or after is None:
            return notes
        gone = {key(n) for n in before}
        # 変換の警告は、開始のときもほかのメッセージより先に並ぶ
        return after + [n for n in notes if key(n) not in gone]

    def _source_notes(self, plan: list[dict], links: list,
                      builds: dict, frozen_of: dict) -> list[dict]:
        """条件と映像ソースの参照先を確認し、見つからないものを知らせる。

        手順に書いてあるのは名前だけで、実体はこの PC の設定から探す
        (file-format.md §8-2)。読み込みで拒否しないのは、参照の
        先を作る前に手順を保存できなくなるからである。だから開始の前に見る。

        見つからなくても開始は拒否しない。参照先が見つからない選択肢は、評価が
        失敗した周期を不一致として扱うだけで、選択肢は「選択条件」が何であっても
        人が押せる。止まったままにはならないので、拒否する理由がない。拒否すると、
        別の PC で作られた手順は、条件をインポートするまで 1 回も動かせなくなる。
        開始の前に知らせて、人が選んで進められるようにする。

        見るのは 5 つである。条件の名前が conditions/ にあること、その条件の
        設定のファイル(condition.json)を読み込めること、その設定に基準画像が書いて
        あること、条件の判定する映像が見つかること(装置に設定している映像なら
        そのレーンが映像を持つこと)、スクリプトへ渡す映像ソースの名前が見つかる
        ことである。見つかった映像も、当てはまるウィンドウが 2 つ以上あれば、取り込む
        ときと同じ理由で知らせる(sources.source_problem)。画像のファイルは
        読まない。開始のたびに、手順が使う条件の画像をすべて読むことになるから
        である。読み込めない条件(形式の違う
        ファイル、壊れたファイル)も、見つからない条件と同じく知らせるだけにする。
        """
        notes: list[dict] = []
        said: set = set()

        def say(dev: str, msg: str) -> None:
            # レーンのメッセージなので、画面はそのレーンのメッセージの場所に出す。
            # 同じ条件を選択肢や周で何度も使う手順があるので、同じ文は
            # 1 回だけにする
            if (dev, msg) in said:
                return
            said.add((dev, msg))
            notes.append({"dev": dev, "line": 0, "msg": msg})

        for p, link in zip(plan, links, strict=True):
            dev = p["dev"]
            store = ConditionStore(self.project.sent.frozen_root(frozen_of[dev][0]))
            have = set(store.names())
            # 装置に設定している映像は、条件とスクリプトで同じ読み方をする
            # (validate_video)。読めなければ、どちらにも使わない
            try:
                own = validate_video(link.cfg.get("video"))
                own_ok = True
            except (ValueError, TypeError):
                own, own_ok = None, False
            for meta in builds[dev].awaits:
                for name in meta.get("conds") or []:
                    if not name:
                        continue
                    if name not in have:
                        say(dev, f"条件「{name}」が見つかりません。"
                                 "この選択肢は自動で選択されません。")
                        continue
                    try:
                        refs = store.has_refs(name)
                        src = validate_source(store.source_of(name))
                    except Exception:   # noqa: BLE001  手で書いた条件のファイルは何でも起こしうる
                        # 判定のときも同じ理由で読み込めず、毎回不一致になる。
                        # 想定していない例外もメッセージにとどめる。抜けると、条件の
                        # ファイル 1 つのために開始が「予期しないエラー」で拒否される
                        say(dev, f"条件「{name}」を読み込めません。"
                                 "この選択肢は自動で選択されません。")
                        continue
                    if not refs:
                        # 基準画像がない条件は何にも一致しない
                        say(dev, f"条件「{name}」に基準画像がありません。"
                                 "この選択肢は自動で選択されません。")
                        continue
                    if src["kind"] == "lane" and not own_ok:
                        # 手で書き換えた padcue.json の、この装置の映像の設定が
                        # 読めない。判定のときも同じ理由で判定できない
                        say(dev, f"条件「{name}」の判定に使用する、この装置の"
                                 "映像の設定を読み込めません。この選択肢は自動で"
                                 "選択されません。")
                        continue
                    seen = (src if src["kind"] != "lane"
                            else own["source"] if own else None)
                    if seen is None:
                        say(dev, f"条件「{name}」の判定に必要な映像が、"
                                 "この装置に設定されていません。この選択肢は"
                                 "自動で選択されません。")
                    elif not source_present(seen):
                        # 別の PC で作った条件は、指定した機器やウィンドウが
                        # この PC にないことがある(file-format.md §10-3)。
                        # 装置に設定している映像も、判定のときに探す映像で確認する。
                        # 映像機器の一覧を確認できないときは、ないと書かずに
                        # 確認できない理由を書く
                        unknown = _dshow_unknown(seen)
                        say(dev, sentences(
                            f"条件「{name}」が判定する映像を、この PC で確認"
                            "できません", unknown,
                            "この選択肢は自動で選択されません") if unknown else
                            f"条件「{name}」が判定する映像が、この PC で"
                            "見つかりません。この選択肢は自動で選択されません。")
                    elif why := source_problem(seen):
                        # 当てはまるウィンドウが 2 つ以上あると、取り込みは毎周期
                        # 拒否する。選択待ちに入る前に、取り込むときと同じ理由で示す
                        say(dev, sentences(
                            f"条件「{name}」が判定する映像を、この PC で 1 つに"
                            "決められません", why, "この選択肢は自動で選択されません"))
                names = list((meta.get("script") or {}).get("capture") or [])
                for guard in meta.get("guard_scripts") or []:
                    names += list((guard or {}).get("capture") or [])
                for name in names:
                    if not isinstance(name, str):
                        continue        # テストが映像ソースの指定を直接書く形
                    lane_video = normalize_source_name(LANE_VIDEO_NAME)
                    if normalize_source_name(name) == lane_video:
                        if not own_ok:
                            # 条件の判定と同じく、読めない設定はメッセージにとどめる
                            say(dev, "この装置の映像の設定を読み込めないため、"
                                     f"スクリプトに「{LANE_VIDEO_NAME}」を渡せません")
                        elif own is None:
                            say(dev, "この装置に映像が設定されていないため、"
                                     f"スクリプトに「{LANE_VIDEO_NAME}」を渡せません")
                        elif not source_present(own["source"]):
                            unknown = _dshow_unknown(own["source"])
                            say(dev, sentences(
                                f"「{LANE_VIDEO_NAME}」をこの PC で確認できません",
                                unknown, "スクリプトには渡しません") if unknown else
                                f"「{LANE_VIDEO_NAME}」がこの PC で"
                                "見つかりません。スクリプトには渡しません。")
                        elif why := source_problem(own["source"]):
                            say(dev, sentences(
                                f"「{LANE_VIDEO_NAME}」をこの PC で 1 つに"
                                "決められません", why, "スクリプトには渡しません"))
                        continue
                    found = find_source_by_name(name)
                    if found is None:
                        unknown = dshow_unknown()
                        say(dev, sentences(
                            f"映像ソース「{name}」をこの PC で確認できません",
                            unknown, "スクリプトには渡しません") if unknown else
                            f"映像ソース「{name}」がこの PC で見つかりません。"
                            "スクリプトには渡しません。")
                    elif why := source_problem(found):
                        say(dev, sentences(
                            f"映像ソース「{name}」をこの PC で 1 つに決められません",
                            why, "スクリプトには渡しません"))
        return notes

    def _note_start_error(self, link, why: str) -> None:
        """装置が開始を拒否したことを覚え、実行記録にも残す。

        装置は待機中のままなので、装置の状態を読むだけでは「開始できなかった
        こと」が分からない。次にそのレーンを開始するまで、サーバーがこのフラグを
        持つ。操作画面を起動し直しても残るよう、runstate.json に書く。
        """
        with self._lock:
            self._state["start_errors"][link.registered_id] = why
            self._save_runstate()
        self._log(link, "PC_START_REJECTED", why=why)

    def _rollback_started(self, names: list) -> list[str]:
        """開始が揃わなかったときの後始末(次の区切りで停止させる)。

        3 種すべてのマスクなので、止まる先はラベルでも周の境でも選択待ちでも
        よい。「今の周の終わり」(周の境だけのマスク)は連動停止の中だけで
        使う止め方である。予約を送れなかったレーンの名前を返す。そのレーンは
        動き続けるので、拒否の文で停止させることを求める(_rollback_text)。
        """
        failed = []
        for name in names:
            try:
                l2 = self._link(name)
                l2.write_through(status=l2.call(
                    lambda c: (c.stop_at_mark(), c.status())[1]))
            except Exception:   # noqa: BLE001  見えないレーンは停止させようがない
                failed.append(name)
        return failed

    # ---- グループの停止 ----

    def stop_lanes(self, mode: str, devs: list | None = None) -> dict:
        """停止の操作(停止、区切りで停止、予約の取り消し)。対象は指したレーンだけで
        ある(_stop_targets)。

        指さなかったときは、動いているグループのレーンが対象になる。押したレーンが
        押した止め方で停止しても、連動停止のトリガーにしない(_mark_hand_stop)。
        """
        if mode not in ("immediate", "graceful", "cancel"):
            # 知らない止め方を黙って停止にしない。指示されていないことを
            # するより、拒否するほうがよい(「今の周の終わり」は連動停止の中だけ
            # で使う止め方で、人が指示するものではない)
            return {"error": "停止の方法が正しくありません"}
        if devs is not None and not devs:
            # 指したレーンが 1 本もない(画面が押した時点で送る対象がなかった)。動いて
            # いるグループのレーン全部と読み替えない
            return {"ok": True, "lanes": []}
        members = self._stop_targets(devs)
        if not members:
            return {"error": "一括操作の対象になるレーンが見つかりません"}
        ids = {link.registered_id for link in members}
        if mode == "cancel":
            # 「予約を取り消す」は、停止の予約(人の区切りで停止、連動停止の予約)だけを
            # 取り消す。周回の終え方は ▾ のメニューで選ぶ別の操作で、やめるのは「元の
            # 周回数に戻す」である。終え方を選んだグループのレーンには送らない。送ると、
            # 一緒に選んだ別のグループの予約を取り消すつもりの人が、目印も出ていない
            # 終え方まで失い、そのグループは元の周回数まで動き続ける(終え方のための
            # 次の選択待ちか周の境で停止する予約も、装置からは停止の予約と同じに見える)
            ending = self.lap_ends.ending(ids)
            members = [link for link in members if link.registered_id not in ending]
            ids -= ending
            if not members:
                return {"ok": True, "lanes": []}
        # 予約で止まって待っていたレーンも、押した止め方で記録する(「区切りで停止」は
        # 装置の止まり方のままでよい)
        pressed = (proto.STOP_REASON_STOPPED if mode == "immediate" else "")
        errs, kinds, failed, later = [], [], [], []
        if mode != "cancel":
            # 一部のレーンを停止させるとき、そのグループの周回の終え方の続きを始めている
            # 途中なら、始め終えるのを待つ(LapEnds.wait_restart)。押した止め方は、続きの
            # グループの記録に置く。待ちきれなかったときの扱いは stop_of が決める
            self.lap_ends.wait_restart(ids)
        # 送り終えるまで、このレーンが停止しても連動停止のトリガーにしない。先に送った
        # レーンが区切りの上(選択待ち)にいると、受けたその場で停止する。監視がそれを
        # 見て、まだ送っていないレーンへ連動停止を送ると、人の止め方で停止するレーンが
        # 連動と記録される。周回数を変える予約をやめるより前に付ける(やめた後は、
        # 予約で停止したばかりのレーンを、監視が普通の停止とみなす)。
        # 取り消しは停止させないので、印を付けない
        held = set() if mode == "cancel" else ids
        self._mark_stopping(held)
        before: dict = {}
        # 取り消しを送れたレーンと、送れなかったので終え方から外したことを戻したレーン
        cancelled: set = set()
        undone: set = set()
        # 送り終えたレーン(送れた、送れずに戻ったら送ることにした、戻した、送るものが
        # なかった)
        done: set = set()
        backs: list = []
        try:
            unread: set = set()
            if mode != "cancel":
                # 周回数を変える予約は、人の止め方に置き換わる(その止め方で停止し、
                # 続きから始め直さない)。終え方に加わっている全レーンを押したら
                # 終え方をやめ、一部のレーンなら押したレーンだけを終え方から外す。
                # 続きを始めている途中なら、外さずに止め方を保存し、続きを始めた後に
                # そのレーンだけを停止させる(LapEnds.stop_of)。予約の取り消しは止め方
                # ではないので、どれもしない(続きを始めている途中のレーンは、もう終え方
                # のない続きのグループへ移っていることがあり、上で外せない。続きを中止
                # するフラグを立てると、予約の取り消しで続きを中止してしまう)
                unread, backs = self.lap_ends.stop_of(ids, stopped=pressed, mode=mode)
            else:
                # 停止する周に入るのを待つ連動停止は、装置にまだ予約がない。装置の予約を
                # 解除するのと同じく、ここで取り消す。応答がなくて送れず、戻ったら送る
                # ことにした「区切りで停止」も同じである
                self._cancel_later(ids)
                self._cancel_kept(ids)
            # 押したレーンの記録に止め方を置く(hand_stop)。そのレーンがこの止め方で
            # 停止しても、連動停止のトリガーにしない(_stops_seen)。区切りで停止は停止
            # するまでに時間がかかるので、送り終えた後の印(_stopping)ではなく、記録に
            # 残す。押したレーンが区切りの上にいると受けたその場で停止するので、送る前に
            # 置く。
            # 終え方から外した後に置く(外す前に続きが始まると、レーンの記録は続きの
            # グループの新しい記録になる)。取り消しは、区切りで停止の止め方を外す
            before = self._mark_hand_stop(ids, mode)
            # 応答のあるレーンへ先に送る。応答のないレーンへの送信は、つなぎ直しを
            # 待ってから失敗する(数秒かかる)。登録情報の順に送ると、後ろのレーンへの
            # 停止がその分遅れ、「区切りで停止」では 1 つ先の区切りまで進む
            for link in sorted(members, key=lambda link: bool(link.error)):
                was_busy = self._lane_busy(link)
                try:
                    if (link.error and mode == "cancel"
                            and not (link.status or {}).get("stop_at_mark")):
                        # 応答がなく、最後に読めた状態で装置に予約のないレーン(PC の
                        # 側にしかない予約は、上で取り消した)。送るものがない。送ると、
                        # 取り消しは済んでいるのに、取り消せないと表示することになる
                        done.add(link.registered_id)
                        continue
                    if link.gone:
                        # 未検出(GONE_S 秒より長く応答がない)と分かっているレーンには
                        # 送らない。送ると、つなぎ直しの待ちの後に失敗し、押した人はその
                        # 間(数秒)、押せたのかが分からない。止め方は残し、戻ったら監視が
                        # 送る(_send_kept_stop)。理由は収集が最後に失敗した理由で示す
                        last = link.error_exc
                        raise (last if isinstance(last, OSError)
                               else TimeoutError("応答がありません"))
                    link.write_through(status=link.call(
                        lambda c, mode=mode: (_stop_call(c, mode), c.status())[1]))
                    done.add(link.registered_id)
                except (DeviceError, OSError) as e:
                    done.add(link.registered_id)
                    # 装置の拒否も OS の失敗も、内部の符号(NOT_OWNER など)と英語を
                    # 出さずに示す
                    why = failure_text(e, link.cfg.get("host") or "")
                    errs += [_STOP_FAILED[mode][0].format(link.cfg.get("name")), why]
                    kinds.append(failure_of(e))
                    failed.append(link.cfg.get("name") or "")
                    # 周回を終える予約で止まって待っていたレーンも、1 回の実行の
                    # 途中なので、動いていたレーンと同じく、戻ったら送る止め方を残す
                    kept = (self._keep_stop(link, mode)
                            if (mode in _STOP_LATER
                                and (was_busy or lapend.held(link.status or {}))
                                and (link.error or isinstance(e, OSError)))
                            else "")
                    if kept:
                        # 予約で止まって待っていたレーンは、戻ったらそのレーンの実行が
                        # 終わる(装置へは何も送らない。_send_kept_stop)。どちらの止め方
                        # でも、停止すると返す
                        later.append((link.cfg.get("name") or "",
                                      kept if was_busy else "immediate"))
                    else:
                        # 送れず、残しもしなかった。装置は押した止め方で停止しないので、
                        # 記録の止め方と、終え方から外したことを、押す前に戻す
                        self._restore_hand_stop(link.registered_id, before)
                        if mode != "cancel":
                            self.lap_ends.undo_leave({link.registered_id})
                            undone.add(link.registered_id)
                    continue
                if link.registered_id in unread:
                    # PC がまだ読んでいなかったが、予約で止まっていたなら、
                    # 待っていたレーンと同じくトリガーにしない(押した止め方と押した
                    # 時刻で記録するのは、drop_of が置いた待つ間の終わり)
                    self.lap_ends.held_if_stopped(link)
                if mode == "graceful" and was_busy:
                    self._take_over(link)
                if mode == "cancel":
                    cancelled.add(link.registered_id)
            if cancelled:
                # 周回の終え方を待つ間に「区切りで停止」で終え方から外したレーンは、
                # 続きが始まる前なら計画に戻す(LapEnds.ask_rejoin)
                self.lap_ends.ask_rejoin(cancelled)
            # 終え方から外したレーンの、周回数を元に戻した行(戻したレーンには書かない)
            self.lap_ends.log_backs([r for r in backs if r[0] not in undone])
        except BaseException:
            # 送る途中で予期しない例外が出た。まだ送り終えていないレーンは、記録の
            # 止め方と、終え方から外したことを押す前に戻してから、例外を上げる(戻さない
            # と、そのレーンが予約で停止したときに終え方をやめてしまう)。送り
            # 終えたレーンの周回数を元に戻した行は書く
            rest = ids - done
            for dev in rest:
                self._restore_hand_stop(dev, before)
            if mode != "cancel" and rest:
                self.lap_ends.undo_leave(rest)
            self.lap_ends.log_backs([r for r in backs
                                     if r[0] in done and r[0] not in undone])
            raise
        finally:
            self._unmark_stopping(held)
        if errs:
            # どのレーンかを文の頭で示し、レーンごとに理由を続ける。応答がないレーンは、
            # 戻ったときに停止することを書く(押し直さなくても、戻ったら監視が送る)。
            # ほかのレーンには、押し直すことを求める。失敗の種類は、コマンドと同じく
            # 最初に失敗したレーンで決める。送れなかったレーンの名前も返す。画面は、その
            # レーンが動いていなくなったら文を消す
            #
            # 戻ったら送る止め方は、押した止め方ではなく _keep_stop が残した方で書く
            # (先に「停止」を残していれば「停止」)。予約で止まって待っていたレーンは、
            # 戻ったら実行が終わると書く(上で later に immediate を入れた所)
            then = [_STOP_LATER[m].format("・".join(n for n, k in later if k == m))
                    for m in _STOP_LATER if any(k == m for _n, k in later)]
            if len(later) < len(failed):
                then.append(_CANCEL_WHEN_BACK
                            if mode == "cancel" and set(kinds) == {"unreachable"}
                            else _STOP_FAILED[mode][1])
            return {"error": sentences(*errs, *then),
                    "failure": kinds[0], "failed": failed}
        # 停止したレーン。指定のない停止(コマンド行)が、どれを停止したかを示す
        return {"ok": True,
                "lanes": [link.cfg.get("name") or "" for link in members]}

    def _mark_hand_stop(self, ids: set, mode: str) -> dict:
        """人が押した止め方を、レーン ids の記録(グループの記録のレーンの hand_stop。
        runstate.json に残る)に置く。押す前の値 {装置 ID: 値(なければ None)} を返す
        (送れなかったときに戻す。_restore_hand_stop)。

        記録に止め方を持つレーンが、その止め方で停止したこと(停止か区切りで停止)は、
        連動停止のトリガーにしない(_by_hand)。人がレーンを指して停止させるのは、その
        レーンだけを停止させるためである。グループの見出しはグループの全レーンを指すので、
        全レーンに置く。止め方は強い方を残す(「停止」の後の「区切りで停止」は「停止」
        のまま)。取り消し(cancel)は、区切りで停止の止め方を外す。予約がなくなった
        レーンが後で区切りで停止すれば、装置のボタンの短押しなど、人がレーンを指して
        押したのでない止め方である。
        """
        before = {}
        with self._lock:
            changed = False
            for g in self._state.get("groups") or []:
                for x in g.get("lanes") or []:
                    dev = x.get("dev")
                    if dev not in ids or x.get("ended"):
                        continue
                    was = x.get("hand_stop")
                    before[dev] = was
                    if mode == "cancel":
                        if was == "graceful":
                            del x["hand_stop"]
                            changed = True
                    elif was != "immediate" and was != mode:
                        x["hand_stop"] = mode
                        changed = True
            if changed:
                self._save_runstate()
        return before

    def _restore_hand_stop(self, dev: str, before: dict) -> None:
        """レーン dev の記録の止め方を、押す前の値 before(_mark_hand_stop)に戻す。"""
        if dev not in before:
            return
        with self._lock:
            for g in self._state.get("groups") or []:
                for x in g.get("lanes") or []:
                    if x.get("dev") != dev or x.get("ended"):
                        continue
                    if before[dev] is None:
                        x.pop("hand_stop", None)
                    else:
                        x["hand_stop"] = before[dev]
                    self._save_runstate()
                    return

    @staticmethod
    def _by_hand(lane: dict, why: str) -> bool:
        """レーンの記録 lane のレーンが、人がそのレーンを指して押した止め方で停止したか
        (_mark_hand_stop)。そのときは連動停止のトリガーにしない。

        押した止め方で停止したとみなすのは、止まり方が停止か区切りで停止のときだけ
        である。
        異常停止、装置のボタンの長押し、止まり方の分からない終わり(起動し直し)、完了は、
        止め方を押した後でも装置の側で停止したので、残りのレーンを停止させる。
        """
        return bool(lane.get("hand_stop")) and why in _HAND_REASONS

    def _keep_stop(self, link, mode: str) -> str:
        """応答がないために人の止め方 mode を送れなかったレーンに、戻ったときに送る
        止め方を置く(グループの記録のレーンの stop_later。runstate.json に残る)。
        置いた止め方を返す(置かなければ空)。

        もう「停止」を置いてあれば、後から「区切りで停止」を押しても「停止」のままに
        する(返すのも「停止」)。つながっていれば、先に押した「停止」ですぐ停止し、後の
        「区切りで停止」は何も起こさない。それと同じ結果にする。「区切りで停止」の後に
        「停止」を押したら「停止」に替える。

        押した人の意図はそのレーンを停止させることである。送れなかったレーンが戻った後に
        動き続けると、人が離れている間に装置が手順の最後まで Switch の操作を続ける。
        未検出の間に押した止め方も、ここに残る。送るのは、戻ったのを見た監視である
        (_send_kept_stop)。操作画面のサーバーを起動し直しても、記録に残るので送る。

        戻る前に次のどれかが起きたら取り消す。そのレーンを開始し直した(レーンの記録が
        替わる)、割り当てを解除した(外したレーンの記録から除く。
        _drop_unregistered_lanes)、実行の終わりを記録した(戻ったら停止していた。
        _mark_lane_ended)。
        """
        dev = link.registered_id
        with self._lock:
            for g in reversed(self._state.get("groups") or []):
                for x in g.get("lanes") or []:
                    if x.get("dev") != dev:
                        continue
                    if x.get("ended"):
                        return ""
                    if x.get("stop_later") != "immediate":
                        x["stop_later"] = mode
                        self._save_runstate()
                    return x["stop_later"]
        return ""

    def _cancel_kept(self, ids: set) -> None:
        """人が「予約を取り消す」を押した。応答がなくて送れず、戻ったら送ることにした
        「区切りで停止」(レーンの記録の stop_later。_keep_stop)のうち、ids のレーンの
        ものを取り消す。

        まだ装置へ送っていないので、未検出の間も取り消せる。残した「停止」は予約では
        ないので、そのままにする。周回を終えるために止まって待っていたレーンは、
        押したときに待つ間を終えていて(戻ったらそのレーンの実行が終わる)、取り消す
        ものがないので、そのままにする。
        """
        by_id = self._by_id()
        with self._lock:
            hit = False
            for g in self._state.get("groups") or []:
                for x in g.get("lanes") or []:
                    link = by_id.get(x.get("dev"))
                    if (x.get("dev") in ids and x.get("stop_later") == "graceful"
                            and not x.get("ended") and link is not None
                            and not lapend.held(link.status or {})):
                        del x["stop_later"]
                        hit = True
            if hit:
                self._save_runstate()

    def _send_kept_stop(self, gid: str, link) -> None:
        """送れなかった人の止め方(stop_later)を、戻ったレーンへ送る(_keep_stop)。

        応答があり、そのレーンの実行がまだ動いていれば送る。停止していた、ほかの手順の
        実行を始めていたなら、送らずにやめる。送れなかったら、次の周期に送り直す。
        送っている間は、停止しても連動停止のトリガーにしない(人の止め方で停止する。
        stop_lanes と同じ)。
        送り終えたら、残した止め方が送ったものと同じときだけ消す。送っている間に人が
        強い止め方を押し、それもこのレーンへ送れずに残した止め方が替わったら
        (_keep_stop)、消さずに次の周期に送る。
        """
        dev = link.registered_id
        cur = self._owned_lane(gid, dev)
        mode = (cur or {}).get("stop_later")
        if not mode or cur.get("ended") or link.error:
            return
        if self._lane_busy(link) and not self._ran_elsewhere(
                link, cur, link.status or {}):
            self._mark_stopping({dev})
            try:
                link.write_through(status=link.call(
                    lambda c: (_stop_call(c, mode), c.status())[1]))
            except (DeviceError, OSError):
                return            # 次の周期に送り直す
            finally:
                self._unmark_stopping({dev})
            if mode == "graceful":
                self._take_over(link)
        with self._lock:
            x = next((x for x in (self._group_by_id(gid) or {}).get("lanes") or []
                      if x.get("dev") == dev), None)
            if x is not None and x.get("stop_later") == mode:
                del x["stop_later"]
                self._save_runstate()

    def _take_over(self, link) -> None:
        """人が「区切りで停止」を送ったレーンに、完了に連動した周の終わりの予約があれば、
        その予約は人の止め方で置き換わった。連動停止の予約の取り消しの行を書く。

        次のどちらの予約も、その一覧から外し、連動停止の予約がなくなった対象
        (linked_stop の cancelled。人が予約を取り消した対象と同じ)に入れる。
        - 装置へ送った後の予約(止まる先の一覧 pending にある)
        - 停止する周に入るのを待つ間の予約(待つ対象 later にある。装置にはまだ
          予約がない)
        区切りで停止しても、連動ではなく人の区切りで停止として記録する(_linked_cause)。
        画面は連動停止の予約と表示しなくなる。

        取り消しの行は、予約の行と同じく停止する周を書くか書かないかを決め
        (_note_link_cancel)、送ったその場で書く。選択待ちのレーンは送ったその場で
        停止し、監視が予約の変化を読む前に停止しうる。後で人が予約を取り消しても、
        連動停止の予約はもうないので、行は書かない。連動停止のほかの止め方(次の
        区切りで)は、人の区切りで停止と同じ早さなので、置き換えにならない。
        """
        dev_id = link.registered_id
        gone = []
        # 待つ対象の追加と削除と、その予約と取り消しの行の並びは、_later_lock で揃える
        # (_send_later、_cancel_later)
        with self._later_lock:
            with self._lock:
                for g in self._state.get("groups") or []:
                    ls = g.get("linked_stop") or {}
                    if _LINKED_MODE.get(ls.get("reason") or "") != "lap":
                        continue
                    if dev_id in (ls.get("pending") or []):
                        ls["pending"] = [d for d in ls["pending"] if d != dev_id]
                        ls["cancelled"] = [*(ls.get("cancelled") or []), dev_id]
                        said = dev_id in (ls.get("sent_later") or [])
                    elif dev_id in (ls.get("later") or []):
                        ls["later"] = [d for d in ls["later"] if d != dev_id]
                        ls["cancelled"] = [*(ls.get("cancelled") or []), dev_id]
                        self._end_if_settled(g)
                        said = True
                    else:
                        continue
                    gone.append((dict(ls), said))
                    self._save_runstate()
            for ls, said in gone:
                self._note_link_cancel(link, ls, said, replaced=True)

    def _note_link_cancel(self, link, ls: dict, lap: bool,
                          at: float | None = None, replaced: bool = False) -> None:
        """連動停止の予約がなくなった行(PC_LINK_CANCEL)を書く。人が予約を取り消した
        ときと、人の「区切りで停止」が完了に連動した周の終わりの予約を置き換えたときに
        書く。

        lap が真なら、停止する周(linked_stop の lap)を書く。予約の行が停止する周を書いた
        レーン(停止する周に入るのを待つ対象と、待った後に予約を送った対象 sent_later)
        である。ログで見比べたときに、同じ予約が同じ書き方になる。at は行の時刻で、
        なければ今の時刻である(置き換えは、置き換わった予約を読んだ時刻か、停止した
        時刻。_clear_pending)。replaced が真なら、人の「区切りで停止」(画面、コマンド、
        装置のボタンの短押し、別のツール)が置き換えた行として印を付ける。誰も取り消して
        いないので、ログは取り消しと書き分ける。
        """
        self._log(link, "PC_LINK_CANCEL",
                  mode=_LINKED_MODE.get(ls.get("reason") or "", ""),
                  cause=ls.get("reason") or "", by=ls.get("cause") or "",
                  **({"lap": ls.get("lap")} if lap else {}),
                  **({"at": at} if at is not None else {}),
                  **({"replaced": True} if replaced else {}))

    def _mark_stopping(self, ids: set) -> None:
        """人の止め方を送っている途中の印を付ける。同じレーンへ 2 つの stop_lanes が
        同時に送ることがあるので、数で持つ。"""
        with self._lock:
            for dev in ids:
                self._stopping[dev] = self._stopping.get(dev, 0) + 1

    def _unmark_stopping(self, ids: set) -> None:
        """_mark_stopping で付けた印を外す。"""
        with self._lock:
            for dev in ids:
                n = self._stopping.get(dev, 0) - 1
                if n > 0:
                    self._stopping[dev] = n
                else:
                    self._stopping.pop(dev, None)

    def _stopping_by_hand(self, links) -> bool:
        """links のどれかへ、人の止め方を送っている途中か。"""
        with self._lock:
            return any(self._stopping.get(link.registered_id) for link in links)

    # ---- グループの解除 ----

    def dissolve_group(self, gid: str) -> dict:
        """停止しているグループを、レーン 1 本ずつのグループに分ける。

        グループは停止しても残るので、まとめて動かした後にレーンを別々に使い
        たいときに、これで解除する。レーンの前回の結果と停止した位置はレーンの記録
        (lanes の要素)が持つので、分けても残る。グループ全体へのメッセージと連動停止の
        記録は、グループがなくなるので持ち越さない。

        動いているレーンがあるグループは分けない。連動停止はグループで行うから
        である(グループを作り直すには先に停止させる、と同じ決まり)。全レーンが
        停止していて、実行の終わりをまだ記録していなければ、ここで記録してから
        分ける。監視は終わりを 1.5 秒かけて確認するが、人は停止したのを見て
        押す。確認し終えるまで解除を拒否すると、停止させた直後に押せず、まだ動いている
        ように見える。
        """
        by_id = self._by_id()

        def refuse():
            g = self._group_by_id(gid)
            if g is None:
                return {"error": "このグループは見つかりません"}
            lanes = list(g.get("lanes") or [])
            # 周回数を変えて続きを待っている間も、1 回の実行の途中である
            if g.get("lap_end") or self._group_active(
                    [x for x in lanes if x.get("dev") in by_id], by_id):
                return {"error": "動いているグループは解除できません。"
                                 "先に停止してください。"}
            return None
        with self._lock:
            bad = refuse()
            lanes = [] if bad else json.loads(json.dumps(
                self._group_by_id(gid).get("lanes") or []))
        if bad:
            return bad
        # 終わりをまだ記録していないレーンは、分ける前に記録する。連動停止のフラグは
        # グループが持つので、分けた後では、連動して停止したと記録できない
        for x in lanes:
            link = by_id.get(x.get("dev"))
            if (link is not None and not link.error
                    and self._mark_lane_ended(gid, x["dev"])):
                first = self._lane_idle_since.get(x["dev"])
                self._note_run_end(link, x, at=self._end_time(
                    link, first[1] if first is not None else time.time()))
        with self._lock:
            bad = refuse()
            if bad:
                return bad
            g = self._group_by_id(gid)
            lanes = list(g.get("lanes") or [])
            if g.get("ended_at") is None:
                g["ended_at"] = _now()
                self._idle_since.pop(gid, None)
            groups = self._state["groups"]
            taken = {x.get("id") for x in groups}
            parts = []
            for x in lanes:
                part = _new_group_id(g.get("started_at") or _now(), taken)
                taken.add(part)
                parts.append({"id": part, "started_at": g.get("started_at"),
                              "ended_at": g.get("ended_at"), "lanes": [x],
                              "linked_stop": None, "notes": []})
            at = groups.index(g)
            groups[at:at + 1] = parts
            self._save_runstate()
        return {"ok": True}

    # ---- 画面へ出す状態 ----
    def last_proc_of(self, dev_id: str) -> str:
        """そのレーンで最後に開始した手順の id。なければ空。

        装置は実行が終わると手順を報告しないので、停止した後に「停止した位置」
        を出すにはこれが必要である。runstate.json にあるので、サーバーを起動し直しても
        見つけられる。
        """
        if not dev_id:
            return ""
        with self._lock:
            groups = self._state.get("groups") or []
            for g in reversed(groups):
                for x in g.get("lanes") or []:
                    if x.get("dev") == dev_id:
                        return str(x.get("proc_id") or "")
        return ""

    def lane_figure(self, dev_id: str, proc_id: str,
                    groups: list | None = None, run: tuple | None = None) -> dict:
        """そのレーンの記録のうち、レーンの図に使う欄(コピー)。

        返すのは {"picks": 選択の記録, "rows": 図の行, "done": 続きの元の実行で終えた
        周(laps_done。なければ None)} である。そのレーンで最後に開始した実行が、装置で
        動いている手順(proc_id)でなければ、どれも空で返す(この padcue が始めていない
        実行は、記録を持たない)。run は装置が返す実行の識別(proto.run_of)で、レーンの
        記録の実行と違えば(同じ手順を別の道具が始め直した)、どれも空で返す。groups は
        groups_copy で写したグループの記録で、渡さなければここで写す。
        """
        if groups is None:
            groups = self.groups_copy()
        for g in reversed(groups):
            for x in g.get("lanes") or []:
                if x.get("dev") != dev_id:
                    continue
                if not dev_id or x.get("proc_id") != proc_id or x.get("ended") \
                        or proto.later_run(x.get("run"), run):
                    return {"picks": [], "rows": [], "done": None}
                return {"picks": [dict(p) for p in x.get("picks") or []],
                        "rows": list(x.get("rows") or []),
                        "done": x.get("laps_done")}
        return {"picks": [], "rows": [], "done": None}

    def _keep_picks(self, gid: str, link, lane: dict) -> None:
        """動いているレーンの、今の周に選んだ選択肢を、レーンの記録に残す。

        レーンの図は、選んだ選択肢の中身を描く(positions の「レーンの図」)。装置は
        選択待ちを抜けた後、どの選択肢へ進んだかを返さないので、分かった時点で残して
        おかないと、操作画面を起動し直した後に、同じ図を描けない。残すのは今の周と、
        周回数のくり返しの外の分だけで、周が替われば前の周の分は捨てる
        (positions.picks_now)。変わったときだけ書く。lane は、このレーンの記録の
        コピーである。
        """
        st = link.status or {}
        dev_id = link.registered_id
        if link.error or lane.get("ended") or st.get("proc") != lane.get("proc_id") \
                or not (st.get("running") or st.get("awaiting")) \
                or _other_run(lane, st):
            # 別の道具が同じ手順を始め直した実行と、古い書き戻しの状態の選択は、この
            # レーンに残さない
            return
        art = self.project.sent.built(lane.get("proc_id") or "", dev_id)
        if art is None:
            return
        try:
            lap = positions.figure_lap(art, st, done=lane.get("laps_done"))
            picks = positions.picks_now(self.project, art, st, lap, dev_id,
                                        lane.get("picks") or [], link.picks_sent,
                                        link.seen_states)
        except positions.BAD_COPY:
            return
        if picks == (lane.get("picks") or []):
            return
        with self._lock:
            cur = next((x for x in (self._group_by_id(gid) or {}).get("lanes") or []
                        if x.get("dev") == dev_id
                        and x.get("proc_id") == lane.get("proc_id")), None)
            if cur is None:
                return
            cur["picks"] = picks
            self._save_runstate()

    def carried_lane(self, cfg: dict) -> dict | None:
        """その行が前の装置から引き継いだ、最後の実行のレーンの記録(コピー)。
        なければ None。

        割り当てを解除した行では、前に割り当てていた装置のレーンのうち最後に開始した
        もの、別の装置を割り当て直した行では、その装置へ移したレーン(sent_dev を
        持つ)である(_drop_unregistered_lanes)。割り当て直した装置で開始すると、
        レーンは新しいグループの記録に替わるので、引き継いだ記録はなくなる。
        返す記録の sent_dev は、送った手順のコピーを探す装置 ID である。
        """
        dev = cfg.get("id") or ""
        olds = set(registry.former_ids(cfg))
        with self._lock:
            for g in reversed(self._state.get("groups") or []):
                for x in g.get("lanes") or []:
                    if dev and x.get("dev") == dev:
                        return dict(x) if x.get("sent_dev") else None
                    if not dev and x.get("dev") in olds:
                        return {**x, "sent_dev": x.get("sent_dev") or x["dev"]}
        return None

    def ended_lane(self, dev_id: str) -> dict | None:
        """その装置で最後に開始したレーンの記録(コピー)。終わりを記録していなければ
        None。

        装置は起動し直すと、止まり方と最後に通った区切りを持たない。起動し直した後に
        まだ実行していない装置の停止した位置は、この記録の終わり(result)から出す
        (guistate)。最後に開始したレーンは last_proc_of と同じ物で、後にこの padcue で
        別の実行を始めれば、そのレーンに替わる。返す記録の sent_dev は、送った手順の
        コピーを探す装置 ID である(carried_lane と同じ)。
        """
        if not dev_id:
            return None
        with self._lock:
            for g in reversed(self._state.get("groups") or []):
                for x in g.get("lanes") or []:
                    if x.get("dev") == dev_id:
                        if not x.get("result"):
                            return None
                        return {**x, "sent_dev": x.get("sent_dev") or dev_id}
        return None

    def groups_copy(self) -> list:
        """グループの記録のコピー。"""
        with self._lock:
            return json.loads(json.dumps(self._state.get("groups") or []))

    def view(self, links: list) -> tuple[list, dict, dict]:
        """画面の状態に使う、グループの記録のコピー、装置の写し({id(link):
        DeviceLink.reading})、開始の途中の値({"start_pending", "start_errors"}。
        snapshot が使う。{"restarting"} は続きを始めている装置で、LapEnds.menus が
        使う)を、同じ時点で取る。1 回の取得の中の読み取り(snapshot、
        LapEnds の holds_and_errors、menus、ends_at、レーンの値)は、これを使う。

        周回を終えるための続きを始めている途中(開始の途中のフラグが立っている)の
        装置は、フラグを立てたときの写しを使う。フラグはこのロックの中で立ち、最初の
        RUN より前で、グループの記録を置き換えるのと同じロックの中で消える。なので、
        ここで取る組は、続きの前の記録と前の状態か、続きの記録と続きの状態のどちらかで
        ある。別々に取ると、止まって待っているレーンと続きを始めたレーンが並び、終了
        予定と周と開始の時刻が出ないか誤る。控えた写しは、同じリンクのときだけ使う
        (改名などでリンクを作り直したら、今のリンクの写しを使う)。
        """
        with self._lock:
            groups = json.loads(json.dumps(self._state.get("groups") or []))
            readings = {}
            for link in links:
                kept = self._start_readings.get(link.registered_id or "")
                same = kept is not None and getattr(kept, "link", kept) is link
                readings[id(link)] = kept if same else link.reading()
            starting = {"start_pending": json.loads(json.dumps(self._start_pending)),
                        "start_errors": dict(self._state["start_errors"]),
                        "restarting": set(self.lap_ends._restarting)}
        return groups, readings, starting

    def snapshot(self, groups: list | None = None, starting: dict | None = None,
                 by_id: dict | None = None) -> dict:
        """画面・CLI へ渡す状態。装置は名前で指して見せる(記録は装置 ID)。

        未登録のレーンは除いて見せるだけで、記録は書き換えない(読み出しの
        経路がファイルを書かないようにする。掃除は監視が行う)。groups、starting、
        by_id({装置 ID: 装置の写し})は view で同じ時点に写したもので、渡さなければ
        ここで読む。
        """
        if groups is None:
            groups = self.groups_copy()
        if by_id is None:
            by_id = self._by_id()
        names = self._names()
        out, lane_group = [], {}
        lane_notes, lane_result, lane_start, lane_done = {}, {}, {}, {}
        # 接続が戻ったら送る人の止め方(_keep_stop)。画面は未検出のレーンの文で示す。
        # 押したときの応答の文は、画面を読み込み直すと消える
        stop_later = {}
        # 周回の終え方の続きを始められなかった理由(レーンの記録にある)
        lap_errs = {}
        for g in groups:
            lanes = [x for x in g.get("lanes") or [] if x.get("dev") in by_id]
            if not lanes:
                continue
            shown = self._group_named(g, lanes, names)
            # 周回数を変える予約は装置 ID で持つ。画面へは menus が渡す
            shown.pop("lap_end", None)
            # 動いているかは、要求のたびに各レーンの状態から求める。周回数を
            # 変えて続きを待っている間も、1 回の実行の途中なので動いている
            shown["active"] = (bool(g.get("lap_end"))
                               or self._group_active(lanes, by_id))
            out.append(shown)
            # 周回の終え方を選んでから続きを始めるまでは、選んだ周回数で開始した
            # ときのメッセージを出す(lapends が選んだ時点で求めて、予約に置く)。周回数の
            # 欄と見出しは選んだ時点で新しい周回数になる。予約をやめたら、動いている
            # 実行のメッセージに戻る
            chosen = (g.get("lap_end") or {}).get("notes") or {}
            for x in lanes:
                nm = names.get(x["dev"], "")
                lane_group[nm] = g.get("id", "")
                lane_notes[nm] = list(chosen.get(x["dev"], x.get("notes")) or [])
                lane_start[nm] = dict(x.get("start_at") or {"kind": "head"})
                if x.get("laps_done") is not None:
                    lane_done[nm] = [x["laps_done"], x.get("laps")]
                if x.get("stop_later") and not x.get("ended") and nm:
                    stop_later[nm] = x["stop_later"]
                if x.get("result"):
                    r = dict(x["result"])
                    r["linked_by"] = self._shown_dev(r.get("linked_by") or "", names)
                    lane_result[nm] = r
                if x.get("lap_end_error") and nm:
                    lap_errs[nm] = x["lap_end_error"]
        # 割り当てを解除した行も、前の装置の最後の実行のメッセージと結果を出す(割り当て
        # 直すと、その装置のレーンの記録として上で出る。carried_lane)
        for link in self.pool.links():
            nm = link.cfg.get("name", "")
            x = None if link.registered_id else self.carried_lane(link.cfg)
            if x is None or not nm:
                continue
            lane_notes[nm] = list(x.get("notes") or [])
            if x.get("result"):
                r = dict(x["result"])
                r["linked_by"] = self._shown_dev(r.get("linked_by") or "", names)
                lane_result[nm] = r
        if starting is None:
            with self._lock:
                starting = {
                    "start_pending": json.loads(json.dumps(self._start_pending)),
                    "start_errors": dict(self._state["start_errors"])}
        errs = {names.get(k, ""): v
                for k, v in starting["start_errors"].items() if names.get(k)}
        lane_start.update({names[k]: dict(v)
                           for k, v in starting["start_pending"].items()
                           if names.get(k)})
        return {"groups": out, "lane_group": lane_group, "start_errors": errs,
                "lap_end_errors": lap_errs,
                "lane_notes": lane_notes, "lane_result": lane_result,
                "lane_start": lane_start, "lane_done": lane_done,
                "lane_stop_later": stop_later}

    def _group_named(self, group: dict, lanes: list, names: dict) -> dict:
        """グループの装置 ID を表示名に置き換えたコピー。

        連動停止のもと(cause)は、前回の実行の連動のもと(linked_by)と同じ呼び名にする
        (_shown_dev。登録を削除した装置は「装置 ID」と下 4 桁)。画面は、予約の
        メッセージと前回の実行とログの行を同じ書き方で作る。
        """
        def nm(dev_id):
            return names.get(dev_id) or dev_id

        def named(ls):
            cause = self._shown_dev(ls.get("cause", ""), names)
            out = {**ls, "cause": cause,
                   **{k: [nm(i) for i in ls.get(k) or []]
                      for k in ("pending", "sent", "resend", "later", "cancelled",
                                "sent_later")}}
            if ls.get("replaced"):
                # 置き換えた前の連動停止の並び(_linked_stop の over)
                out["replaced"] = [named(x) for x in ls["replaced"]]
            return out

        g = dict(group)
        g["lanes"] = [nm(x["dev"]) for x in lanes]
        ls = group.get("linked_stop")
        if ls:
            g["linked_stop"] = named(ls)
        return g

    # ---- 監視(連動停止) ----

    def close(self) -> None:
        self._stop = True

    def _watch_loop(self) -> None:
        while not self._stop:
            try:
                self._watch_once()
            except Exception as e:   # noqa: BLE001  監視は続ける
                self._note_watch_error(e)
            time.sleep(self.POLL_S)

    def _note_watch_error(self, e: Exception) -> None:
        """監視が失敗した理由を記録する(except の中から呼ぶこと)。

        連動停止はこの監視が担当する。監視は続けるが、失敗を記録せずに捨てると、
        毎周期失敗し続けていても、利用者には連動停止が行われないことしか分からず、
        原因を追う手がかりが残らない。記録が溢れないよう、理由が変わったときだけ
        記録する。
        """
        msg = f"{type(e).__name__}: {e}"
        if msg == self._watch_error:
            return
        self._watch_error = msg
        # 登録情報が壊れているときは、画面の見出しが理由を示す
        if isinstance(e, ConfigError):
            return
        traceback.print_exc()
        # どの装置の監視でもないので、装置の名前を付けずに書く(画面のログでは
        # 装置の列が空になる)。書けないときに例外を外へ出すと、呼び出し元の
        # except の中から出るので同じ try では捕捉されず、監視のスレッドごと
        # 終わる(連動停止が行われないままになり、操作画面のサーバーを起動し直す
        # まで戻らない)
        try:
            self.project.logs.append(
                [{"kind": "PC_WATCH_ERROR", "a": 0, "b": 0, "message": msg}])
        except Exception:   # noqa: BLE001  上の注釈のとおり
            pass

    def _watch_once(self) -> None:
        now = time.monotonic()
        by_id = {link.registered_id: link for link in self.members()
                 if link.registered_id}
        self._drop_unregistered_lanes(by_id)
        with self._lock:
            groups = json.loads(json.dumps(self._state.get("groups") or []))
        # 消えたグループの待ち合わせの記録を捨てる(グループの識別子は開始のたびに新しい)
        self._joins.keep(g.get("id", "") for g in groups)
        clean = True
        for g in groups:
            try:
                self._watch_group(g, by_id, now)
            except Exception as e:   # noqa: BLE001  ほかのグループの監視は続ける
                # ここで受けないと、そのグループより後ろのグループが毎周期まったく評価
                # されない(連動停止も終わりの記録も行われなくなる)
                self._note_watch_error(e)
                clean = False
        # ひと回り失敗せずに済んだときだけ理由を消す(グループごとの異常はここで
        # 受けてしまうので、呼び出し元の _watch_loop からは見えない)
        if clean:
            self._watch_error = ""

    def _watch_group(self, g: dict, by_id: dict, now: float) -> None:
        """1 つのグループの 1 周期ぶんの判断。"""
        gid = g.get("id", "")
        links = [by_id[x["dev"]] for x in g.get("lanes") or []
                 if x.get("dev") in by_id]
        if not links or not self._restore_seen(gid, links):
            self._forget_idle(gid)
            return
        self._clear_pending(gid, g, links)
        # レーンごとの周の記録は、グループが閉じたあとも続ける。完了に連動して周の
        # 終わりで停止させるレーンと、自分で完了するレーンは、グループが閉じた後も
        # 実行されるからである
        lane_of = {x.get("dev"): x for x in g.get("lanes") or []}
        # 周回数を変えるために停止させたレーンは、終わりを記録しない(続きから始め直す)。
        # 予約はコピーを取った後に置かれうるので、停止したのを見てから記録を見る
        # (予約は記録を置いてから送るので、予約で停止したならもう記録がある)
        for link in links:
            dev_id = link.registered_id
            self._note_gone(link, lane_of.get(dev_id) or {})
            self._keep_picks(gid, link, lane_of.get(dev_id) or {})
            if (((lane_of.get(dev_id) or {}).get("wait_end") or {}).get("unread")
                    and not link.error):
                # 周回の終え方をやめたときに応答がなかったレーン。予約で周の境に
                # 停止していたかを、戻ったときの状態で見る(止め方を送る前に)
                self.lap_ends.held_on_return(gid, link)
            if (lane_of.get(dev_id) or {}).get("stop_later"):
                # 未検出の間に押された止め方を、戻ったら送る
                self._send_kept_stop(gid, link)
            moving = bool(not link.error and self._lane_busy(link))
            live = self.lap_ends.live(gid)
            if (not moving and live is not None
                    and dev_id in (live.get("plans") or {})):
                # 周回の終え方に加わっているレーンは、停止しても続きを待つ。終え方から
                # 外れたレーン(レーンの止め方で停止した)は、待たずに終わりを書く
                continue
            self._track_lane(gid, link, lane_of.get(dev_id) or {}, moving, now)
        # 応答がないレーンがいる周期は、何も決めない。最後に読めた STATUS の
        # ままなので、そのまま読むと「完了した」「相手が来なくなった」と
        # 間違える(数時間の周回では毎周この間を通る)。判定は次の周期へ持ち越す。
        # 本当の異常なら、装置が異常を報告するか完了しない
        if any(link.error and not link.gone for link in links):
            self._forget_idle(gid)
            return
        joiners = [link for link in links if not link.error]
        busy = {k.registered_id: self._lane_busy(k) for k in joiners}
        # この周期に、停止した相手に連動してその場か次の区切りで停止させるなら、合流点を
        # 進めない(hold)。相手が停止すると、待っているレーンには「まだ来る可能性のある
        # レーン」がなくなるので、待ち合わせは進めてよいと答える。進めると、停止させる
        # までの間に先の入力を送り(ログには「待たずに進みました」と残る)、区切りで
        # 停止させる連動停止では周の終わりまで進んで、相手と停止した位置がずれる。
        # 合流点で待っているレーンはもう区切りの上にいるので、進めなければその場で
        # 停止する。
        # - 連動停止を送れず、送り直しを待っているレーン(linked_stop の resend)も、
        #   同じ止め方なら進めない(送り直すより先に進めると、同じことが起きる)
        # - 連動停止を始めた後も、後から停止したレーンに連動して停止させるなら同じである
        if self.lap_ends.live(gid) is not None:
            stopping = None
        elif not self._closed(g):
            stopping = self._stops_seen(joiners, busy, gid=gid)
        elif g.get("linked_stop"):
            stopping = self._stops_seen(joiners, busy, keep=self._own_stop(g), gid=gid)
        else:
            stopping = None
        hold: set = set()
        if stopping and _LINKED_MODE[stopping[1]] != "lap":
            hold = {link.registered_id for link in links}
        ls = g.get("linked_stop") or {}
        if _LINKED_MODE.get(ls.get("reason") or "", "immediate") != "lap":
            hold |= set(ls.get("resend") or [])
        # 人の止め方を、戻ったら送るレーンも進めない(_send_kept_stop が送れなかった)
        hold |= {x.get("dev") for x in g.get("lanes") or [] if x.get("stop_later")}
        # 合流点の待ち合わせは、グループの終わりを記録した後も進める。「完了」の連動
        # 停止で周の終わりを予約されたレーンは、グループが閉じた後もその周の終わりまで
        # 実行される。途中の合流点で待ったままだと、予約した周の終わりに着かない(周の
        # 境だけの予約は選択待ちでは停止しない)。未検出のレーンも待つ相手に残すので、
        # すべてのレーンを渡す
        self._tick_joins(gid, g, links, busy, hold=hold)
        if self._closed(g):
            # 1 回の実行の終わりを記録済みか、連動停止を始めたグループである。終わりは
            # 二度記録しない。連動停止は続ける(このモジュールの説明の止め方の契約)。
            # 後から停止したレーンに連動して、今の予約より早く停止するレーンを置き換え
            # (_linked_stop の over。トリガーは _own_stop で選ぶ)、続けて、送れなかった
            # 対象へ送り直し、停止する周に入るのを待つ対象へ予約を送る。早さは即時、次の
            # 区切り、周の終わりの 3 段なので、予約を取り消さない限り、置き換えは
            # 多くても 2 度で終わる
            self._forget_idle(gid)
            if g.get("linked_stop"):
                stopped = self._just_stopped(links, keep=self._own_stop(g), gid=gid)
                if stopped is not None and self._linked_stop(
                        gid, links, stopped[0], _LINKED_WHY[stopped[1]],
                        mode=_LINKED_MODE[stopped[1]], reason=stopped[1],
                        over=True):
                    # 送り直す対象と待つ対象は、置き換えた連動停止で次の周期から
                    # 見る(g は置き換える前のコピーである)
                    return
            self._resend_linked(gid, g, links)
            self._send_later(gid, g, links)
            return
        le = self.lap_ends.live(gid)
        if le is not None and self.lap_ends.watch(gid, {**g, "lap_end": le}, links):
            # 周回数を変えるために停止させている間は、連動停止も実行の終わりも決めない
            self._forget_idle(gid)
            return
        self._watch_active(gid, g, links, busy, now)

    def _forget_idle(self, gid: str) -> None:
        """全員停止を最初に見た時刻を消す。解除(dissolve_group)も同じ欄を消すので、
        読み書きは self._lock の下で行う。"""
        with self._lock:
            self._idle_since.pop(gid, None)

    def _watch_active(self, gid: str, g: dict, links: list,
                      busy: dict, now: float) -> None:
        """まだ閉じていないグループの 1 周期ぶんの判断。"""
        # 連動停止。トリガーは「グループの 1 台が停止したこと」で、
        # どう停止したかで残りの止め方が決まる。装置のボタンも、別のツールからの停止も
        # 同じに扱う。人が画面かコマンドでレーンを指して押した止め方で停止したレーンは
        # トリガーにしない(そのレーンだけを停止するために押した。グループの見出しは全
        # レーンを指す)。「未検出」はトリガーにしない(グループに残り、ほかは続行する)
        stopped = self._just_stopped(links, gid=gid)
        if stopped is not None and self.lap_ends.live(gid) is not None:
            return        # 周回数を変える予約で停止した。次の周期で続きを始める
        if stopped is not None:
            dev_id, why = stopped
            self._linked_stop(gid, links, dev_id, _LINKED_WHY[why],
                              mode=_LINKED_MODE[why], reason=why)
            return
        # 実行の終わり(グループの誰も動いていない)。収集は毎秒なので、1.5 秒続けて
        # 見えたときだけ確定する(1 回の取りこぼしで終わったと判定しない)
        if not any(busy.values()):
            with self._lock:
                first = self._idle_since.setdefault(gid, now)
            if now - first >= _END_CONFIRM_S and self.lap_ends.live(gid) is None:
                self._forget_idle(gid)
                self._finish_group(gid)
            return
        self._forget_idle(gid)

    def lane_records(self, gid: str) -> dict:
        """グループ gid の今のレーンの記録のコピー {装置 ID: 記録}。"""
        with self._lock:
            return {x.get("dev"): dict(x)
                    for x in (self._group_by_id(gid) or {}).get("lanes") or []}

    def _just_stopped(self, links, keep=None,
                      gid: str | None = None) -> tuple | None:
        """この周期で停止したレーンと、その止まり方を返す。

        「動いていた」から「動いていない」への変化だけを見る。停止した
        ままの装置を毎周期のトリガーにすると、連動停止を送り続けることになる。
        応答がない間は判定しない。最後に読めた STATUS のままなので、
        停止したと間違える(「未検出」はトリガーではない)。

        同じ周期に 2 本以上が停止して見えたら、止め方の強いレーン(その場で、次の
        区切りで、今の周の終わりでの順)を返す。収集は装置ごとに毎秒、監視は 0.5 秒
        ごとなので、2 本の変化を 1 回の監視で見ることがある。並びの先で決めると、
        1P の完了と 2P の異常停止を一度に見たとき、残りは異常停止に連動せず、今の周の
        終わりまで動く。止め方が同じなら、並びの先のレーンである。

        keep(リンク, 止まり方)を渡すと、それが真を返すレーンだけから選ぶ(連動停止を
        始めた後のトリガー。_own_stop)。gid(グループの識別子)を渡すと、人がそのレーンを
        指して押した止め方で停止したレーン(_by_hand)を選ばない。止め方は、状態を読んだ
        後にグループの今の記録から読む(監視が周期の初めに取ったコピーは、その後に押した
        止め方を持たない。止め方は送る前に記録に置くので、押した止め方で停止した状態を
        読んだなら、記録にはもうある)。

        人の止め方(stop_lanes)をこのレーンのどれかへ送っている途中なら、何も返さず、
        変化も覚えない。送った先から停止したレーン(区切りの上にいた)をトリガーに
        すると、まだ送っていないレーンを連動停止で停止させ、人の止め方で停止するレーンを
        連動と記録する。変化は送り終えてから見る。そのときには送り終えたレーンの
        記録に止め方があるので、そのレーンは選ばない。印は状態を読んだ後で見る。読む
        前に見ると、見た後に送り始めた人の止め方で停止したレーンを、印のないまま読む
        ことがある。
        """
        # 応答のないレーンは見ない。次の周期へ持ち越す
        live = [link for link in links if not link.error]
        busy = {link.registered_id: self._lane_busy(link) for link in live}
        if self._stopping_by_hand(links):
            return None
        got = self._stops_seen(live, busy, keep, gid)
        self._was_busy.update(busy)
        return got

    def _stops_seen(self, links, busy: dict, keep=None,
                    gid: str | None = None) -> tuple | None:
        """busy(装置 ID -> 動いているか)で見て、前の周期から停止したレーンと、その
        止まり方を返す(_just_stopped と同じ選び方)。前の周期の記録は変えない。

        合流点の待ち合わせ(_watch_group)が、この周期に連動停止を始めるかを先に
        知るのに使う。links は応答のあるレーンである。
        """
        lanes = self.lane_records(gid) if gid is not None else {}
        got = []
        for link in links:
            dev_id = link.registered_id
            if self._was_busy.get(dev_id, False) and not busy.get(dev_id, False):
                why = (link.status or {}).get("stop_reason") or ""
                if self._by_hand(lanes.get(dev_id) or {}, why):
                    continue
                if why in _LINKED_MODE and (keep is None or keep(link, why)):
                    got.append((dev_id, why))
        return min(got, key=lambda x: _MODE_ORDER.index(_LINKED_MODE[x[1]]),
                   default=None)

    # ---- 監視の補助関数 ----

    def _drop_unregistered_lanes(self, by_id: dict) -> None:
        """登録情報から消えたレーンをグループから外し、装置を交換したレーンを新しい
        装置へ移す。

        残り 1 本になったグループはそのまま 1 台のグループとして残し、0 本に
        なったグループは消す。登録情報へ同じ装置 ID で登録し直しても、グループには
        戻らない。
        グループの決まりは「抜ける」としか定めておらず、戻す規則がないからである。

        割り当てを解除した行の装置(行の「前に割り当てていた装置」)のレーンは、
        終わりを記録した実行なら外さない。装置が壊れて交換し、停止した所から続けるのは、
        長い周回で普通に起きる。外すと、前回の実行と停止した位置が消え、先頭から
        やり直すことになる。行に別の装置を割り当てたら、レーンをその装置へ移す。
        送った手順のコピーは前の装置の分なので、その装置 ID を sent_dev に残す
        (carried_lane)。終わりを記録していない実行(動いている間に未検出になって
        解除した)は外す。外したレーンの記録は保持し(_parked)、同じ装置を割り当て直した
        ときに、装置がまだその実行を続けていれば担当し直す(_take_back_parked)。
        """
        # 前に割り当てていた装置 ID -> その行のいまの装置 ID(空 = 未割り当て)
        rows = {}
        for link in self.pool.links():
            for old in registry.former_ids(link.cfg):
                rows[old] = link.registered_id
        with self._lock:
            kept, changed = [], False
            taken = {x.get("dev") for g in self._state.get("groups") or []
                     for x in g.get("lanes") or []}
            for g in self._state.get("groups") or []:
                lanes = []
                for x in g.get("lanes") or []:
                    dev = x.get("dev")
                    new = rows.get(dev)
                    if dev in by_id or (new == "" and x.get("ended")):
                        lanes.append(x)
                    elif new and x.get("ended") and new not in taken:
                        x["dev"] = new
                        x.setdefault("sent_dev", dev)
                        taken.add(new)
                        lanes.append(x)
                        changed = True
                    elif new == "":
                        # 動いている間に割り当てを解除した。同じ装置を割り当て直した
                        # ときに担当し直せるよう、記録を保持する。戻ったら送る止め方
                        # は、割り当ての解除で取り消す(_keep_stop)。未検出になった時の
                        # 記録は、担当し直したときに戻った時の行を書くので残す
                        x.pop("stop_later", None)
                        self._parked[dev] = (x, g.get("started_at"))
                        changed = True
                    else:
                        self._forget_lost(dev)
                        changed = True
                if len(lanes) != len(g.get("lanes") or []):
                    g["lanes"] = lanes
                if lanes:
                    kept.append(g)
                else:
                    changed = True
            ids = {g.get("id") for g in self._state.get("groups") or []}
            changed = self._take_back_parked(by_id, rows, kept, ids) or changed
            if changed:
                # 実際に変わったときだけ書く(毎周期ファイルを書かない)
                self._state["groups"] = kept
                self._save_runstate()
            gids = {g.get("id") for g in kept}
            # 解除(dissolve_group)も同じ欄を消すので、同じロックの中でたどる
            for gid in [k for k in self._idle_since if k not in gids]:
                del self._idle_since[gid]
        for key in [k for k in self._restored
                    if k.split("/", 1)[0] not in gids]:
            self._restored.discard(key)

    def _forget_lost(self, dev: str) -> None:
        """グループから外したレーンの、未検出になった時と戻った回数の記録を捨てる。
        self._lock の下で呼ぶ。

        未検出になった後の戻りは、このレーンではもう記録しない(登録し直した装置の
        最初の開始で「未検出 →」の行を書かない)。戻った回数も、次にこの装置を見た
        ときから数え直す(割り当てを解除した行の収集は、未検出のまま成功に戻るので
        回数が増える。それを登録し直した装置の最初の開始で読むと、未検出になっていない
        実行に未検出になった時と戻った時の行を書く)。
        """
        self._lane_gone.pop(dev, None)
        self._lane_back_count.pop(dev, None)

    def _take_back_parked(self, by_id: dict, rows: dict, groups: list,
                          ids: set) -> bool:
        """割り当てを解除して外したレーン(_parked)のうち、同じ装置を割り当て直したもの
        を、装置がまだその実行を続けていれば 1 本のグループとして担当し直し、続けて
        いなければ保持した記録を捨てる。groups はこれから残すグループの並びで、担当し
        直したグループを足す。ids は使用済みのグループの識別子である。self._lock の下で
        呼ぶ。何か変えたら真を返す。

        続けているかは、装置の実行中の手順の id がレーンの手順の id と同じで、その装置へ
        送った手順のコピーが見つかることで見る(周と終わりはそのコピーで読む)。
        グループには戻さない(登録し直しても戻らない決まり。_drop_unregistered_lanes)。
        担当し直した後は、ほかのレーンと同じく周と終わりを記録に書き、未検出になって
        いたなら戻った時の行も書く(_note_gone)。停止していた、別の手順を動かして
        いた、開始し直した(開始の途中か、新しいレーンの記録がある)なら、その実行の
        終わりは分からないので書かない。
        割り当てを解除した行に別の装置を割り当てたか、行の登録を削除したときも捨てる。
        """
        changed = False
        held = {x.get("dev") for g in groups for x in g.get("lanes") or []}
        for dev, (x, started_at) in list(self._parked.items()):
            link = by_id.get(dev)
            if link is None and rows.get(dev) == "":
                continue            # 行は割り当てを解除したまま
            again = dev in held or bool(self._starting.get(dev))
            st = (link.status or {}) if link is not None else {}
            if link is not None and not again and (link.error or not st):
                continue            # 割り当て直した装置を、まだ読めていない
            del self._parked[dev]
            changed = True
            if (link is not None and not again
                    and (st.get("running") or st.get("awaiting"))
                    and st.get("proc") == x.get("proc_id")
                    and not self._ran_elsewhere(link, x, st)
                    and self.project.sent.built(x.get("proc_id") or "", dev)
                    is not None):
                gid = _new_group_id(_now(), ids | {g.get("id") for g in groups})
                groups.append({"id": gid, "started_at": started_at,
                               "ended_at": None, "lanes": [x],
                               "linked_stop": None, "notes": []})
                held.add(dev)
            else:
                self._forget_lost(dev)
        return changed

    def _restore_seen(self, gid: str, links: list) -> bool:
        """グループのレーンを STATUS と 1 回ずつ照合し、揃ったら True を返す。

        起動直後は収集がまだ 1 度も行われておらず、status が空である。そのまま
        「全員停止」とみなすと、動き続けているグループに終了時刻を書いてしまう。生死が
        分からない間は判断を次の周期へ持ち越す。実行中に
        サーバーを終了しても装置は自分で動き続けるからである。

        復元で装置へ送るものはない。STOP も RUN も送らない。起動そのものが
        実行を停止させてはならないからである。停止した理由(完了、停止、
        区切りで停止、物理ボタン、異常)は、STATUS の欄がそのまま
        /api/state のレーンに出るので、ここで読み直すものはない。

        「未検出」と判定が付いた相手は、生死が分からないのではなく判定が
        済んでいるので、応答がないままでも待たない。待つと、起動した時点で
        消えていた装置が 1 台あるだけで、そのグループの残りのレーンの監視
        (周の記録、連動停止、合流点の待ち合わせ)がいつまでも始まらない。
        未検出のレーンはグループに残り、ほかのレーンは続行する(合流点では待つ相手に
        残る。coupling.md §1)。
        """
        ready = True
        for link in links:
            key = f"{gid}/{link.cfg.get('id', '')}"
            if key in self._restored:
                continue
            if (link.status and not link.error) or link.gone:
                self._restored.add(key)
            else:
                ready = False
        return ready

    def _clear_pending(self, gid: str, g: dict, links: list) -> None:
        """すぐには停止しない連動停止(区切り・周の終わり)の行方を追う。

        停止し終えたレーンを pending から外すだけである。着いた先で停止させるための
        操作はしない。選択待ちもラベルも周の境も装置にとっては区切りで、
        予約を受けた装置はその場で自分で停止する。PC が停止を重ねて
        送ると、「今の周の終わりで停止する」と表示しながら途中で停止させることに
        なる。

        周の終わりの予約(周の境だけ)が、停止する前に別の予約に替わったら、人が予約を
        上書きした(装置の上面のボタンの短押しか、別のツールからの「区切りで停止」)。
        予約を取り消したときと同じく、連動停止の予約がなくなった対象(cancelled)に
        入れる。停止しても連動とは記録せず(_linked_cause)、トリガーにする(_own_stop)。
        完了の前に押したときと同じく、人の「区切りで停止」として扱う(coupling.md 1 節の
        表の「区切りで停止」の行)。

        画面とコマンドの「区切りで停止」は、送ったその場で覚える(_take_over)。読む前に
        停止したときは、装置が返す停止させた予約(stopped_mask)で判別する
        (_linked_cause)。

        予約を取り消したときと、人の予約で上書きしたときは、連動停止の予約がなくなった
        ことを取り消しの行で残す(_note_link_cancel)。行は、今の記録の止まる先の一覧に
        まだあったレーンにだけ書く。_take_over が先に外していたら、そちらが書いている。
        """
        ls = g.get("linked_stop") or {}
        pending = list(ls.get("pending") or [])
        if not pending:
            return
        lap = _LINKED_MODE.get(ls.get("reason") or "") == "lap"
        ended, cancelled = [], []
        # 人の予約で置き換わった対象(取り消しの行に置き換えの印を付ける)。予約が消えた
        # 対象は、人が取り消した
        replaced = set()
        by_dev = {}
        # 取り消しの行の時刻(装置 ID -> 時刻)。置き換わった予約を読んだ時刻か、読む前に
        # 停止したなら停止した時刻にする。監視が気づいた時刻にすると、装置がもう停止した
        # 後になることがあり、停止した行の後に並ぶ(停止してから取り消したように読める)
        said_at = {}
        for link in links:
            dev_id = link.registered_id
            if dev_id not in pending or link.error:
                continue
            st = link.status or {}
            by_dev[dev_id] = link
            if not st.get("running") and not st.get("awaiting"):
                ended.append(dev_id)     # 停止した。止まる先の一覧(pending)から外す
                if (lap and st.get("stop_reason") == proto.STOP_REASON_AT_MARK
                        and st.get("stopped_mask") != binfmt.MARK_LAP):
                    # 人の「区切りで停止」(装置の上面のボタンの短押しなど)が予約を
                    # 上書きし、PC がそれを読む前に停止した(停止させた予約が周の境だけで
                    # ない)。読めたときと同じく取り消しの行で残し、停止した行の前に
                    # 並べる
                    cancelled.append(dev_id)
                    replaced.add(dev_id)
                    said_at[dev_id] = (link.run_ended_at or time.time()) - 0.001
                continue
            said_at[dev_id] = link.status_at or time.time()
            if not st.get("stop_at_mark"):
                # 実行中のまま予約が消えた。人が「区切りで停止」をもう一度押して
                # 取り消した。その後の止まり方は連動ではない(_linked_cause)
                cancelled.append(dev_id)
            elif lap and st.get("stop_at_mark") != binfmt.MARK_LAP:
                cancelled.append(dev_id)     # 人の予約で上書きした
                replaced.add(dev_id)
        if not (ended or cancelled):
            return
        with self._lock:
            cur = self._group_by_id(gid)
            cur_ls = (cur or {}).get("linked_stop")
            if not cur_ls:
                return
            # 人の「区切りで停止」(_take_over)は別のスレッドから同じ欄を書く。
            # 読んだときのコピーで書き戻さず、外した対象だけを今の記録から外す
            still = set(cur_ls.get("pending") or [])
            cancelled = [d for d in cancelled if d in still]
            gone = {*ended, *cancelled}
            cur_ls["pending"] = [d for d in cur_ls.get("pending") or []
                                 if d not in gone]
            cur_ls["cancelled"] = [*(cur_ls.get("cancelled") or []), *cancelled]
            said = {k: cur_ls.get(k) for k in ("reason", "cause", "lap")}
            waited = set(cur_ls.get("sent_later") or [])
            self._save_runstate()
        # 停止する周に入るのを待ってから予約を送ったレーン(sent_later)は、予約の行が
        # 停止する周を書いた(「3 周目の終わりで」)ので、取り消しの行も同じく書く
        for dev_id in cancelled:
            self._note_link_cancel(by_dev[dev_id], said, dev_id in waited,
                                   at=said_at.get(dev_id),
                                   replaced=dev_id in replaced)

    def _linked_stop(self, gid: str, links, cause_id: str, why: str,
                     mode: str = "immediate", reason: str = "",
                     over: bool = False) -> bool:
        """連動停止。停止させる対象か待つ対象がいて、連動停止を記録に置いたら真を返す。

        cause_id は原因の装置の ID、mode は残りの止め方、reason は原因の止まり方
        (stop_reason)である。止め方は原因の止まり方で決まる(_LINKED_MODE)。相手が
        即時に停止したのに自分が今の周の終わりまで実行すると、2 台で揃えていた意味が
        なくなる。記録の重さ(異常か)は reason で決める。文の言い回しで決めると、装置の
        名前に「異常」を含めただけで赤くなる。

        over は、連動停止を始めた後に停止したレーンに連動する、置き換えの連動停止で
        ある(_watch_group)。まだ動いているレーンのうち、今の予約(なし、周の終わり、
        次の区切り)より早く停止する止め方になるレーンだけを対象にし(_linked_need の
        take_lap)、同じか遅い止め方なら何もせず、記録も書かない。周の終わりの予約は、
        次の区切りかその場の止め方で上書きし、周を待つレーンは待つのをやめる(置き
        換えた記録の later には入らない)。今の連動停止は、置き換えた記録の replaced の
        末尾に足す(古い順)。置き換える前の予約で停止したレーンの終わりは、その予約の
        連動停止のもとで記録する(_linked_cause)。

        対象ごとの扱いは、次の関数が担当する。
        - 今送るか、周を待つか、送らないか: _linked_need(完了に連動するときは、完了した
          レーンと同じ周回数まで実行してから停止させる)
        - 人の止め方を送れず、戻ったら送るレーン(stop_later): _kept_stop_covers
        - 送れなかった対象(linked_stop の resend): _resend_linked
        - 完了に連動して、停止する周に入るのを待つ対象(linked_stop の later):
          _send_later。装置に予約できるのは次の周の境で停止することだけなので、その周に
          入る前のレーンにはまだ送らず、その周に入ってから送る。予約の行は、待つ対象
          にも今書き、停止する周を書く
        - 送り直す対象も待つ対象も残らないときの、グループの終わりの時刻:
          _end_if_settled
        """
        with self._lock:
            cur = self._group_by_id(gid) or {}
            lane_of = {x.get("dev"): dict(x) for x in cur.get("lanes") or []}
            before = dict(cur.get("linked_stop") or {}) if over else {}
        at_lap = (self._finished_laps(lane_of.get(cause_id) or {})
                  if mode == "lap" else None)
        others = [link for link in links if link.registered_id != cause_id
                  and not self._kept_stop_covers(
                      lane_of.get(link.registered_id) or {}, mode)]
        need = {link.registered_id: self._linked_need(
                    link, mode, lane_of.get(link.registered_id) or {}, at_lap,
                    take_lap=over)
                for link in others if not link.error}
        live = [link for link in others if need.get(link.registered_id) == "now"]
        later = [link for link in others if need.get(link.registered_id) == "later"]
        # 未検出のレーンも対象にする。未検出のレーンもグループに残り、停止した台が
        # あれば残りも停止する(coupling.md 1 節)。最後に読めた状態で動いていたなら、
        # 送り直す対象(linked_stop の resend)に入れ、戻ったときに同じ止め方を送る
        # (_resend_linked)
        missing = [link for link in others
                   if link.error and self._lane_busy(link)]
        if over:
            # 置き換えでは、応答のないレーンも、最後に読めた予約より早く停止する止め方に
            # なるときだけ対象にする
            missing = [link for link in missing if self._linked_need(
                link, mode, lane_of.get(link.registered_id) or {}, at_lap,
                take_lap=True)]
        # 停止させるのは、連動停止を送る時点でまだこのグループに属し、新しい実行を
        # 始めている途中ではないレーンだけである。人が停止させた直後に始め直すと、
        # 監視がその停止を見る前に、新しい開始が RUN を送り始めることがある。
        # 対象を限定しないと、前の実行が停止したことをトリガーにして、始めたばかりの
        # 実行を停止させてしまう。動いて見えるなら RUN は済んでいて、フラグはその前に
        # 立っているので、絞り込みは「動いているか」を見た後で行う
        with self._lock:
            members = {x.get("dev") for x in
                       (self._group_by_id(gid) or {}).get("lanes") or []}

            def ours(link) -> bool:
                return (link.registered_id in members
                        and not self._starting.get(link.registered_id))
            live = [link for link in live if ours(link)]
            later = [link for link in later if ours(link)]
            missing = [link for link in missing if ours(link)]
        if not live and not missing and not later:
            # 停止させる対象が 1 台もいなければ何もしない。人がグループごと停止させた
            # ときのように、残りがもう停止している(または、もう止まる先が決まって
            # いる)ことがある。そこで記録を残すと、runstate.json に「相手が停止した
            # ので停止させました」という起きなかった停止が並ぶ
            return False
        if before and (_MODE_ORDER.index(mode) > _MODE_ORDER.index(
                _LINKED_MODE.get(before.get("reason") or "", "immediate"))):
            # 置き換える前の連動停止をまだ送っていない対象に、それより遅い止め方を
            # 送ることになるなら、置き換えない(早い方を残す)。置き換える前の連動停止が、
            # その対象へ早い止め方を送る(即時の連動停止の後に動いているのは、まだ
            # 送っていない対象だけなので、ほかに停止させる対象もいない)
            waiting = set(before.get("resend") or [])
            if any(link.registered_id in waiting for link in (*live, *later, *missing)):
                return False
        cause_name = next((k.cfg.get("name") for k in links
                           if k.registered_id == cause_id), cause_id)
        # 送れなかったレーンも送り直す対象に入れる。記録(PC_LINK_STOP)と止まる先の
        # 一覧(pending)、送れた対象(sent)には、送れたレーンだけを入れる。区切りか周の
        # 終わりで停止させる止め方を受けてその場で停止したレーンは、送れた対象にだけ
        # 入れる(_send_linked)
        pending, sent = [], []
        resend = [link.registered_id for link in missing]
        for link in live:
            if not self._send_linked(link, mode, cause_id, cause_name, why, reason):
                resend.append(link.registered_id)
                continue
            sent.append(link.registered_id)
            if mode != "immediate" and self._lane_busy(link):
                # すぐには停止しない。止まる先を記録に残す(その場で停止したレーンは
                # 入れない。_send_linked)
                pending.append(link.registered_id)
        # 待つ対象の予約の行は、取り消し(_cancel_later)より前に並ぶよう、待つ対象を
        # 記録に置くのと同じロックの中で書く
        with self._later_lock:
            with self._lock:
                cur = self._group_by_id(gid)
                if cur is None:
                    return True
                prev = (cur.get("linked_stop") or {}) if over else {}
                replaced = ([*(prev.get("replaced") or []),
                             {k: v for k, v in prev.items() if k != "replaced"}]
                            if prev else [])
                cur["linked_stop"] = {"cause": cause_id, "why": why,
                                      "reason": reason,
                                      "at": _now(), "pending": pending,
                                      "sent": sent, "resend": resend,
                                      "later": [k.registered_id for k in later],
                                      "lap": at_lap,
                                      **({"replaced": replaced} if replaced else {})}
                self._end_if_settled(cur)
                self._save_runstate()
            for link in later:
                self._note_linked(link, mode, cause_id, cause_name, why, reason,
                                  lap=at_lap)
        return True

    @staticmethod
    def _finished_laps(lane: dict) -> int | None:
        """完了したレーンが終えた周回数。周を持たない実行(周回数のくり返しより後ろから
        始めた)と、周回数が分からないときは None である。

        周回を終えるために、くり返しの後ろから続けた実行は、続ける前に終えた周回数
        (laps_done)である。
        """
        if lane.get("laps_done") is not None:
            return lane["laps_done"]
        laps = lane.get("laps")
        return None if lane.get("nolaps") or not isinstance(laps, int) else laps

    def _lap_where(self, link, lane: dict) -> tuple[bool, int | None]:
        """そのレーンが周回数のくり返しを抜けたかと、いま何周目か。

        くり返しに入る前は 0 周目とする。抜けた所の区切りも抜けた側に数える(そこは
        周の境なので、周の境で停止する予約を送ると後処理を飛ばす)。周を持たない実行
        (くり返しの後ろから始めた)は抜けた側である。送った手順のコピーが見つからなければ
        (False, None) を返す。
        """
        art = self.project.sent.built(lane.get("proc_id") or "", link.registered_id)
        if art is None:
            return False, None
        lm = (link.status or {}).get("last_mark") or {}
        if positions.past_laps(art.lap_map, lm.get("index")):
            return True, None
        lap = Project.lap_of(art.lap_map, lm)
        return False, (0 if lap is None else lap)

    def _linked_need(self, link, mode: str, lane: dict,
                     at_lap: int | None = None, take_lap: bool = False) -> str:
        """そのレーンに連動停止の止め方を送るか(応答のあるレーンについて見る)。

        "now" は今送る、"later" は完了したレーンの周回数(at_lap)の周に入ってから
        送る、"" は送らない。

        停止しているレーンには送らない。区切りか周の終わりで停止させる連動停止では、
        もう止まる先が決まっている(予約のある)レーンにも送らない。

        完了に連動するときは、残りのレーンも完了したレーンと同じ周回数まで実行して
        から停止させる(coupling.md 1 節)。周が揃っていないグループで今の周の
        終わりで停止させると、遅れたレーンの最後の回と後処理を削る。
        - 周回数が at_lap 以下のレーンと、周回数のくり返しを抜けたレーンは、自分で
          最後の回と後処理まで実行して完了するので送らない。自分の最後の周を実行して
          いるレーンにも送らない(そこで完了する)。送ると、誰も押していない予約が出る
        - 周回数が無限か at_lap より多いレーンは、at_lap の周に入っていれば今送る(その
          周の終わりで停止する。もう過ぎていれば今の周の終わり)。入る前なら待つ
        - 何周目か分からないレーンと、完了したレーンの周回数が分からないときは、今
          送る(今の周の終わりで停止する)。待つと、いつまでも停止しない

        take_lap が真なら、完了に連動した周の終わりの予約(周の境だけ)を持つレーンにも
        送る(置き換えの連動停止。_linked_stop の over)。その予約より早く停止する止め方
        で上書きする。次の区切りで停止する予約(人の「区切りで停止」も、連動停止のもの
        も)は、区切りで停止させる止め方では上書きしない(同じ早さである)。
        """
        if not self._lane_busy(link):
            return ""
        mask = (link.status or {}).get("stop_at_mark")
        if mode != "immediate" and mask and not (take_lap and mask == binfmt.MARK_LAP):
            return ""
        if mode != "lap":
            return "now"
        laps = lane.get("laps")
        after, lap = self._lap_where(link, lane)
        if after or (laps is not None and (
                lap == laps or (at_lap is not None and laps <= at_lap))):
            return ""
        if at_lap is None or lap is None or lap >= at_lap:
            return "now"
        return "later"

    def _send_linked(self, link, mode: str, cause_id: str, cause_name: str,
                     why: str, reason: str, note: bool = True) -> bool:
        """1 本のレーンへ連動停止の止め方を送る。送れたら記録に残し、真を返す。

        区切りか周の終わりで停止させる止め方を受けて、その場で停止したレーン(区切りの
        上にいた。選択待ち・合流点で待っていた)には、予約の行を書かない。停止した後に
        「次の区切りで」停止する予約の行が並ぶと、もう一度動いて次の区切りで停止する予定
        だったと読める。停止したことと連動したことは、終わりの行が示す(その場での
        止め方の指示の行を、画面が畳むのと同じ)。動いていたレーンが、送った直後に読んだ
        ときにもう、送った予約で区切りに停止していたら、行を書く(送ってから読むまでの間に
        次の区切りに着いた。予約のとおりに停止した)。動いていたかは、送る前に読む。
        読めなければ、送った直後の状態だけで決める。note が偽なら記録に
        書かない(周に入るのを待った予約は、待ち始めたときに書いてある。_send_later)。
        """
        moving = False
        if note and mode != "immediate":
            try:
                before = link.call(lambda c: c.status())
                moving = bool(before.get("running")) and not before.get("awaiting")
            except (DeviceError, OSError):
                pass
        try:
            link.write_through(status=link.call(
                lambda c, mode=mode: (_stop_call(c, mode), c.status())[1]))
        except (DeviceError, OSError):
            return False                  # 送り直す(_resend_linked)
        if mode != "immediate" and not self._lane_busy(link):
            st = link.status or {}
            reached = (moving and st.get("stop_reason") == proto.STOP_REASON_AT_MARK
                       and st.get("stopped_mask") == (
                           binfmt.MARK_LAP if mode == "lap" else binfmt.MARK_ALL))
            if not reached:
                return True
        if note:
            self._note_linked(link, mode, cause_id, cause_name, why, reason)
        return True

    def _note_linked(self, link, mode: str, cause_id: str, cause_name: str,
                     why: str, reason: str, lap: int | None = None) -> None:
        """連動停止の指示か予約の行を書く。lap は、その周に入るのを待つ予約の、停止
        する周(その周の終わりで停止する)である。"""
        # もとのレーン(by)は装置 ID で残す。画面は今の名前で示す(終わりの記録の
        # linked_by と同じ。名前で残すと、後で名前を変えたときに同じ実行の行どうしが
        # 一致しない)
        extra = {"lap": lap} if lap is not None else {}
        self._log(link, "PC_LINK_STOP",
                  a=1 if mode == "immediate" else 0, mode=mode,
                  why=f"{cause_name}: {why}", cause=reason, by=cause_id, **extra)

    def _resend_linked(self, gid: str, g: dict, links: list) -> None:
        """連動停止を送れなかった対象(linked_stop の resend)へ送り直す。

        未検出の間は待つ。応答があり、まだ動いていて止まる先が決まっていなければ、
        同じ止め方を送る。次のレーンには送らずに、対象から外す。
        - 停止していたレーン(自分で停止した、完了した)
        - 止まる先がもう決まっているレーン
        - グループを抜けたか、新しい実行を始めているレーン
        - 人の止め方を送れず、戻ったら送ることになったレーン(stop_later)のうち、
          この止め方がそれより早くないもの(_kept_stop_covers)
        完了に連動して、完了したレーンの周回数の周に入る前のレーンは、待つ対象
        (later)へ移し、予約の行を書く(_linked_stop と同じ)。対象が残らなくなったら、
        グループの終わりの時刻を書く(_end_if_settled)。
        """
        ls = g.get("linked_stop") or {}
        resend = list(ls.get("resend") or [])
        if not resend:
            return
        reason = ls.get("reason") or ""
        mode = _LINKED_MODE.get(reason, "immediate")
        cause_id = ls.get("cause") or ""
        cause_name = next((k.cfg.get("name") for k in links
                           if k.registered_id == cause_id), cause_id)
        by_dev = {link.registered_id: link for link in links}
        lane_of = {x.get("dev"): x for x in g.get("lanes") or []}
        left, pending, sent, later = [], [], [], []
        for dev_id in resend:
            link = by_dev.get(dev_id)
            with self._lock:
                starting = bool(self._starting.get(dev_id))
            if (link is None or starting
                    or self._kept_stop_covers(lane_of.get(dev_id) or {}, mode)):
                continue
            if link.error:
                left.append(dev_id)       # 戻るのを待つ
                continue
            # 置き換えの連動停止(replaced がある)は、戻った対象の周の終わりの予約も
            # 上書きする(_linked_stop の over と同じ)
            need = self._linked_need(link, mode, lane_of.get(dev_id) or {},
                                     ls.get("lap"),
                                     take_lap=bool(ls.get("replaced")))
            if need == "later":
                later.append(link)
                continue
            if not need:
                continue
            if not self._send_linked(link, mode, cause_id, cause_name,
                                     ls.get("why") or "", reason):
                left.append(dev_id)
                continue
            sent.append(dev_id)
            if mode != "immediate" and self._lane_busy(link):
                pending.append(dev_id)
        if left == resend:
            return
        with self._later_lock:
            with self._lock:
                cur = self._group_by_id(gid)
                cur_ls = (cur or {}).get("linked_stop")
                if not cur_ls:
                    return
                cur_ls["resend"] = left
                cur_ls["sent"] = [*(cur_ls.get("sent") or []), *sent]
                cur_ls["pending"] = [*(cur_ls.get("pending") or []), *pending]
                cur_ls["later"] = [*(cur_ls.get("later") or []),
                                   *(k.registered_id for k in later)]
                self._end_if_settled(cur)
                self._save_runstate()
            for link in later:
                self._note_linked(link, mode, cause_id, cause_name,
                                  ls.get("why") or "", reason, lap=ls.get("lap"))

    def _send_later(self, gid: str, g: dict, links: list) -> None:
        """完了したレーンの周回数の周に入るのを待つ対象(linked_stop の later)へ、
        その周に入ったら、周の終わりで停止する予約を送る。

        予約の行は待ち始めたときに書いてあるので、送るときは書かない。次のレーンには
        送らずに、対象から外す。
        - 停止したレーン(自分で停止した、人が停止させた)
        - 人が「区切りで停止」を予約したレーン(取り消しの行を書く。下)
        - 自分で完了するようになったレーン
        - グループを抜けたか、新しい実行を始めているレーン
        - 人の止め方を送れず、戻ったら送ることになったレーン(stop_later。周の終わりの
          予約はどの人の止め方より遅いので、いつも当てはまる。_kept_stop_covers)
        未検出の間と送れなかったときは、次の周期にもう一度見る。人が予約を取り消したら、
        stop_lanes が外す(_cancel_later)。待つ対象も送り直す対象も残らなくなったら、
        グループの終わりの時刻を書く(_end_if_settled)。

        人が「区切りで停止」を予約したレーンは、連動停止の予約がその予約で置き換わった
        ので、予約がなくなった対象(cancelled)に入れ、取り消しの行を書く
        (_note_link_cancel)。ここで見つかるのは、装置の上面のボタンの短押しか、別の
        ツールから予約したときである。画面とコマンドからのものは、送ったその場で
        _take_over が外して書いている。

        1 周が監視の周期(収集の毎秒と監視の 0.5 秒)より短いと、その周に入ったのを
        読む前に次の周へ入ることがある。そのときは、もう過ぎた周回数なので、読んだ周の
        終わりで停止する(1 周遅れる)。
        """
        ls = g.get("linked_stop") or {}
        later = list(ls.get("later") or [])
        if not later:
            return
        reason = ls.get("reason") or ""
        mode = _LINKED_MODE.get(reason, "immediate")
        cause_id = ls.get("cause") or ""
        cause_name = next((k.cfg.get("name") for k in links
                           if k.registered_id == cause_id), cause_id)
        by_dev = {link.registered_id: link for link in links}
        lane_of = {x.get("dev"): x for x in g.get("lanes") or []}
        for dev_id in later:
            link = by_dev.get(dev_id)
            with self._lock:
                starting = bool(self._starting.get(dev_id))
            if link is not None and not starting and link.error:
                # 戻るのを待つ。戻ったら送る人の止め方を持つレーンも外さない。戻って
                # その止め方を送ったときに、つながっていたときと同じく、人の予約が
                # 連動停止の予約を置き換えたこととして取り消しの行を書く
                continue
            kept = self._kept_stop_covers(lane_of.get(dev_id) or {}, mode)
            need = ""
            if link is not None and not starting and not kept:
                need = self._linked_need(link, mode, lane_of.get(dev_id) or {},
                                         ls.get("lap"))
            if need == "later":
                continue
            # 動いているのに装置に予約があるのは、人の「区切りで停止」である(待つ対象
            # には、PC はまだ予約を送っていない)
            taken = bool(link is not None and not starting and not kept and not need
                         and self._lane_busy(link)
                         and (link.status or {}).get("stop_at_mark"))
            with self._later_lock:
                with self._lock:
                    cur = self._group_by_id(gid)
                    cur_ls = (cur or {}).get("linked_stop")
                    if not cur_ls or dev_id not in (cur_ls.get("later") or []):
                        continue          # 送る直前に取り消された
                if need and not self._send_linked(link, mode, cause_id, cause_name,
                                                  ls.get("why") or "", reason,
                                                  note=False):
                    continue              # 次の周期に送り直す
                with self._lock:
                    cur_ls["later"] = [d for d in cur_ls.get("later") or []
                                       if d != dev_id]
                    if taken:
                        cur_ls["cancelled"] = [*(cur_ls.get("cancelled") or []),
                                               dev_id]
                    said = {k: cur_ls.get(k) for k in ("reason", "cause", "lap")}
                    if need:
                        cur_ls["sent"] = [*(cur_ls.get("sent") or []), dev_id]
                        if self._lane_busy(link):
                            cur_ls["pending"] = [*(cur_ls.get("pending") or []),
                                                 dev_id]
                            # 予約の行は停止する周を書いた。取り消しの行も周で書く
                            # (_clear_pending)
                            cur_ls["sent_later"] = [
                                *(cur_ls.get("sent_later") or []), dev_id]
                    self._end_if_settled(cur)
                    self._save_runstate()
                if taken:
                    # 行は、人の予約を読んだ時刻で書く(停止した行より前に並べる)
                    self._note_link_cancel(link, said, True,
                                           at=link.status_at or None, replaced=True)

    def _cancel_later(self, ids: set) -> None:
        """人が「予約を取り消す」を押した。周に入るのを待つ連動停止(linked_stop の
        later)のうち、ids のレーンのものを取り消し、記録に残す。

        装置にはまだ予約がないので、装置の予約を解除するだけでは取り消せない。取り消した
        レーンは、装置の予約を取り消したレーンと同じく、連動ではない(cancelled)。
        """
        by_id = self._by_id()
        with self._later_lock:
            hits = []
            with self._lock:
                for g in self._state.get("groups") or []:
                    ls = g.get("linked_stop") or {}
                    hit = [d for d in ls.get("later") or [] if d in ids]
                    if not hit:
                        continue
                    ls["later"] = [d for d in ls["later"] if d not in hit]
                    ls["cancelled"] = [*(ls.get("cancelled") or []), *hit]
                    self._end_if_settled(g)
                    hits += [(d, dict(ls)) for d in hit]
                if hits:
                    self._save_runstate()
            for dev_id, ls in hits:
                link = by_id.get(dev_id)
                if link is not None:
                    self._note_link_cancel(link, ls, True)

    # ---- レーンごとの周の記録 ----

    def _lap_now(self, link, lane: dict, status: dict | None = None) -> int | None:
        """そのレーンがいま何周目か。分からなければ None。

        装置は周回を数えないので、最後に通った区切りと、転送のときに送った手順のコピーへ
        残した対応表から出す。どの成果物で実行しているかはグループの記録が持つ。装置の
        状態が報告する手順は、実行が終わると空になるからである。
        status を渡すと、いまの状態でなくその状態で見る。
        """
        art = self.project.sent.built(lane.get("proc_id") or "", link.registered_id)
        if art is None:
            return None
        st = link.status if status is None else status
        return Project.lap_of(art.lap_map, (st or {}).get("last_mark"))

    def _tick_joins(self, gid: str, g: dict, links: list,
                    busy: dict, hold: set | None = None) -> None:
        """合流点の待ち合わせを 1 周期進める。

        待ち合わせの記録(joinwait)に判断を任せ、進めてよいレーンへ SELECT を送る。
        hold(装置 ID の集まり)のレーンは進めない(連動停止でその場か次の区切りで
        停止させるレーン。_watch_group)。記録は観測のぶんだけ進める。停止したレーンは、
        次の周期に進まずに抜けたと数えられる。

        未検出のレーンも、待つ相手として渡す(joinwait)。ここに来るのは、応答が
        ないレーンが未検出と判定済みの周期だけである(判定の前は何も決めない)。
        まだ来る可能性があるかは、最後に読めた状態で決める。
        """
        lane_of = {x.get("dev"): x for x in g.get("lanes") or []}
        lanes, by_id = [], {}
        for link in links:
            dev_id = link.registered_id
            if not dev_id:
                # 周期の途中で割り当てを解除された装置。相手として数えられない
                continue
            # 開始の途中か、もうこのグループに含まれないレーンは、合流の判断に使わない。
            # 装置は始め直した実行を動かしていて、その周を前の実行の手順の対応表で読むと
            # でたらめになり(4294967294 周目など)、待っている相手を、後の周にいるとして
            # 待たずに進めてしまう。始め直す前のレーンは停止していて、もともと相手として
            # 来ない。状態は確かめる前に読む(_track_lane と同じ)。連動停止のトリガーには
            # 残す(_just_stopped)
            st = link.status or {}
            if self._owned_lane(gid, dev_id) is None:
                continue
            gone = bool(link.error)
            if not gone:
                by_id[dev_id] = link
            lanes.append({"dev": dev_id, "st": st,
                          "lap": self._lap_now(link, lane_of.get(dev_id) or {}, st),
                          "arriving": (self._lane_busy(link) if gone
                                       else busy.get(dev_id, False)),
                          "gone": gone})
        go = [item for item in self._joins.tick(gid, lanes)
              if item["dev"] not in (hold or ())]
        # 進めるレーンの装置の記録を、SELECT を送る前に読んで書く。装置は合流点に着いた
        # ときに待ちに移った状態の行を書いている(周回の終え方の続きが合流点から始まった
        # なら、続きの開始の行も)。次の収集を待つと、受け取った時刻が合流の行より後に
        # なり、合流してから待ちに入ったように読める。全レーンを読んでから送る(1 本ずつ
        # 読んで送ると、レーンの間に読む時間のずれが入る)
        for item in go:
            by_id[item["dev"]].read_logs_now()
        for item in go:
            self._send_join_select(by_id[item["dev"]], item)

    def _send_join_select(self, link, item: dict) -> None:
        """合流点を進める。選択肢は 1 本しかないので arm は 0 である。

        世代を付けるのは、その待ちを装置が時間切れで抜けた直後に受け取った
        SELECT が、次の待ちを勝手に進めないようにするためである。
        拒否されたときは記録も残さない。進めていないからである。
        合流の行の時刻は、SELECT を送る前の時刻にする。送った後に装置が書く
        「待ち → 実行中」は、それより後に受け取るので、合流の行の後に並ぶ(送った後の
        時刻にすると、その間に収集が受け取った行が前に並ぶ)。
        """
        at = time.time()

        def go(c, gen=item["gen"]):
            c.select(0, gen=gen)
            link.note_pick(gen, 0)
            return c.status()
        try:
            link.write_through(status=link.call(go))
        except (DeviceError, OSError):
            return                 # 次の周期で送り直す
        self._log(link, "PC_JOIN", a=item["name"], why=item["why"], at=at)

    def join_waits(self) -> dict:
        """合流点で待っているレーンを、画面へ渡す形で返す。

        {レーンの名前: {"name": 合流点の名前, "waiting_for": [相手の名前]}}
        である。相手は、同じ回に着いておらず、その周より後の周にもいないレーン
        である(joinwait.py の waiting)。
        """
        names = self._names()
        with self._lock:
            gids = [g.get("id", "") for g in self._state.get("groups") or []]
        out = {}
        for gid in gids:
            for dev_id, item in self._joins.waiting(gid).items():
                if names.get(dev_id):
                    out[names[dev_id]] = {
                        "name": item["name"],
                        "waiting_for": [names.get(x) or x
                                        for x in item["waiting_for"]]}
        return out

    def _note_gone(self, link, lane: dict) -> None:
        """動いていたレーンが未検出になった時と、戻った時を記録に残す。

        未検出の間は装置を読めないので、その間に通り過ぎた周の行は、戻った後に経過
        フレーム数から時刻を出して書き足す(_note_laps)。その間は装置を見ていなかった
        ことを読めるよう、未検出になった時と戻った時を状態の行と同じ形で残す
        (画面は「状態: 実行中 → 未検出」「状態: 未検出 → 実行中」と表示する)。
        待機中に見えなくなった装置(使い終えて電源を切った装置など)と、終わりを
        記録した実行のレーンは書かない。

        監視は 0.5 秒ごとに見るので、未検出になってすぐ戻ると、未検出を一度も読まない
        (画面はその間「未検出」と出す)。そのときは、戻った回数(DeviceLink.back_count)が
        増えたことで知り、未検出になった時の行と戻った時の行を続けて書く。未検出になる
        前の状態は、応答がなくなる前に読めた状態で書く。
        """
        dev_id = link.registered_id
        if not dev_id:
            return
        was = self._lane_gone.get(dev_id)
        backs = link.back_count
        seen = self._lane_back_count.get(dev_id)
        self._lane_back_count[dev_id] = backs
        if link.gone:
            if was is None and not lane.get("ended"):
                self._note_lost(link, link.status or {})
            return
        if (was is None and seen is not None and backs > seen
                and not lane.get("ended")):
            was = self._note_lost(link, link.status_at_loss)
        if was is not None and not link.error:
            # 戻ったときの状態で書く。いまの状態は、戻った後の操作(停止など)の
            # 後の値でありうる。戻ったときの状態を持たないリンク(未検出の間に
            # 改名して作り直した行)は、いまの状態で書く。まだ状態を読んでいなければ、
            # 読めるまで待つ(画面は空の状態を「不明」と表示する)
            device = link.state_at_back or (link.status or {}).get("state") or ""
            if not device:
                return
            now = device
            if (now == "IDLE" and lapend.held(link.status or {})
                    and not lane.get("ended") and not lane.get("wait_end")):
                # 周回を終える予約で止まったまま、まだ続きを待っている(1 回の実行の
                # 途中)。未検出になった時の行と同じく実行中と書く
                now = "RUNNING"
            del self._lane_gone[dev_id]
            at = time.time()
            # 終わりの時刻の下限に戻った時刻を置くのは、装置そのものが動いていたとき
            # だけである。予約で止まって待っていたレーンに置くと、最後の周の時間に
            # 未検出の間が入る
            if device in ("RUNNING", "AWAITING"):
                # 戻ったときに動いていた実行は、戻った後に停止する。待ちで止まったまま
                # 戻った装置へ止め方を送ると、経過フレーム数が進んでいないので、収集が
                # 見積もる停止した時刻は戻って最初に読めた時刻になり、この行より前に
                # なる(_after_run_start)
                self._lane_back_at[dev_id] = at
            self._log(link, "PC_BACK", now=now, at=at)

    def _note_lost(self, link, st: dict) -> str | None:
        """装置が状態 st で動いていたか、周回を終える予約で止まって待って
        いたなら、未検出になった時の行を書き、未検出になる前の状態(RUNNING か
        AWAITING。待っていたときは RUNNING)を返す。どれでもなければ何も書かずに
        None を返す。

        行の時刻は、装置の応答がなくなった時刻(DeviceLink.lost_at。最後に読めた時刻の
        後)にする。監視が気づくのは 10 秒後か、短い未検出では戻った後なので、気づいた
        時刻で書くと、未検出の間に起きた行(装置の起動、未検出の間に通り過ぎた周)が
        未検出になった時の行より前に並ぶ。
        """
        if not (st.get("running") or st.get("awaiting") or lapend.held(st)):
            return None
        # 周回を終える予約で止まって待っていたレーンも、1 回の実行の途中である。装置は
        # 待機中だが、実行中と書く(予約で停止した状態の行は書いていないので、待機中と
        # 書くと前の行から続かない。待ちの上で停止したレーンも、待ちに入った行はなく、
        # 前の行は実行中である。続きが始まると、装置が待ちに入った行を書く)
        was = "AWAITING" if st.get("awaiting") else "RUNNING"
        self._lane_gone[link.registered_id] = was
        at = link.lost_at or time.time()
        self._log(link, "PC_GONE", was=was,
                  at=max(at, self._lane_run_at.get(link.registered_id, 0.0)))
        return was

    def _owned_lane(self, gid: str, dev_id: str) -> dict | None:
        """そのグループのレーンが、まだその装置の実行を担当しているなら、いまの記録の
        レーン(コピーではない)を返す。担当していなければ None。

        担当しないのは、装置が開始の途中(開始の途中のフラグが立っている)か、レーンが
        もうそのグループに含まれないときである。装置の状態は、これを呼ぶ前に読むこと
        (_track_lane の注釈)。
        """
        with self._lock:
            if self._starting.get(dev_id):
                return None
            return next((x for x in (self._group_by_id(gid) or {}).get("lanes") or []
                         if x.get("dev") == dev_id), None)

    def _ran_elsewhere(self, link, lane: dict, st: dict) -> bool:
        """装置の状態 st が、このレーンの実行でない実行のものか。

        動いている間の状態の手順の id は、動いている手順のものである(実行中は手順を
        送れない)。停止した後の状態の手順の id は、次の手順を送っただけでも替わるので、
        収集が最後に実行中と読んだ手順の id(DeviceLink.last_proc)で見る。どちらも
        分からなければ偽とする(サーバーを起動し直した直後など)。同じ手順を始め直した
        ことは、装置が返す実行の識別(レーンの記録の run)で見る。
        """
        on = st.get("running") or st.get("awaiting")
        mine, now = lane.get("run"), proto.run_of(st)
        if mine and now:
            # どちらの実行の識別も分かれば、それで決める。後の別の実行なら(同じ
            # 手順の始め直しも)別の実行で、同じ起動で番号が小さい状態は古い書き戻し
            # なので、手順の id で比べ直さない。同じ識別で動いている手順が違うのは、
            # レーンの記録の識別が別の道具の実行のものだったとき(こちらの RUN の
            # 直前に別の道具が始めた)で、別の実行である
            if proto.later_run(mine, now):
                return True
            return bool(on and tuple(mine) == now and st.get("proc")
                        and lane.get("proc_id") and st["proc"] != lane["proc_id"])
        # 起動し直した装置は、実行を始めるまで番号を持たない(run_of が None)ので、
        # 手順の id で見る(起動し直しは、起動し直しとして別に扱う)
        ran = st.get("proc") if on else link.last_proc
        return bool(ran) and bool(lane.get("proc_id")) and ran != lane["proc_id"]

    def _track_lane(self, gid: str, link, lane: dict, busy: bool,
                    now: float) -> None:
        """周が進んだこと、実行が終わったことを記録に残す。"""
        dev_id = link.registered_id
        if not dev_id or not lane:
            return
        # 装置の状態は、このレーンがまだその装置の実行を担当しているかを確かめる
        # 前に読み、確かめた後はその状態を使う。開始は、開始の途中のフラグを立てて
        # から RUN を送り、すべて送った後でグループを作り直す。その間と、監視が
        # グループのコピーを取った後に作り直された周期には、コピーのレーンは前の実行の
        # もので、装置はもう始め直した実行を動かしている。前の実行の手順の対応表で
        # 始め直した実行の区切りを読むと、周がでたらめになる(周回数が無限の対応表で
        # 2 周の手順のカウンターを読むと 4294967294 周目になり、そこまでの周の行を
        # 1 行ずつ書くことになる)。フラグは RUN の前に立ち、レーンがグループを抜ける
        # のは RUN の後なので、読んだのが始め直した実行の状態なら、確かめたときに
        # どちらかに当たる
        st = link.status or {}
        cur = self._owned_lane(gid, dev_id)
        if cur is None or cur.get("ended"):
            # 担当していないレーンと、終わりを記録した実行のレーンは読まない。終わりを
            # 記録したレーンの装置が動いているのは、別の実行である(開始が揃わずに
            # 後始末で次の区切りまで動かした実行や、ほかのツールで始めた実行)
            return
        if not link.error and self._ran_elsewhere(link, lane, st):
            # このレーンの実行の後に、装置が別の手順の実行を始めていた(操作画面の
            # サーバーを通らない --host のコマンドなど)。その区切りは、このレーンの
            # 手順の対応表では読めない。このレーンの実行はもう終わっている
            self._end_run_taken_over(gid, link, lane)
            return
        if busy:
            self._lane_idle_since.pop(dev_id, None)
            lap = self._lap_now(link, lane, st)
            if lap is not None and self._lane_lap.get(dev_id) != lap:
                # 時刻は、監視が読んだ時刻ではなく、装置がその周の区切りを通った
                # 時刻にする。読む周期(収集は毎秒、監視は 0.5 秒ごと)のぶんばらつくと、
                # 同じ長さの周が「3 秒」「5 秒」と出る
                self._note_laps(link, lap, st,
                                self._mark_time(link, st, None, own=True),
                                head=self._lap_head_frame(link, lane, st))
            return
        if link.error:
            return
        # 収集は毎秒なので、1.5 秒続けて停止に見えたときだけ確定する
        # (1 回の取りこぼしで終わったと判定しない)
        first = self._lane_idle_since.get(dev_id)
        if first is None:
            self._lane_idle_since[dev_id] = (now, time.time())
            return
        if now - first[0] < _END_CONFIRM_S:
            return
        # フラグは runstate.json に持つ。サーバーを起動し直しても、終わりを二度は
        # 書かない
        if not self._mark_lane_ended(gid, dev_id):
            return
        # 時刻は装置が停止した時刻にする(_end_time)。確定した時刻にすると、周の
        # 記録から出す「その周にかかった時間」が確定の待ちぶん(1.5 秒)長く出る
        self._note_run_end(link, lane, at=self._end_time(link, first[1]))

    def _end_time(self, link, seen: float) -> float:
        """そのレーンの実行が停止した時刻(UNIX 秒)。seen は初めて停止して見えた時刻。

        収集が、最後に実行中と読んだ時刻とそこから進んだ経過フレーム数から出した時刻
        (DeviceLink.run_ended_at)を使う。監視が停止を見るのは、収集と監視の周期の
        ぶん(最大で約 1.5 秒)遅れるので、それを使うと最後の周と 1 周だけの実行の
        時間が長く出る。止まり方の分からない終わり(実行の途中で装置が起動し直した)と、
        見積もりがないときは seen にする。開始の記録と最後に書いた周の行より前には
        しない(早送りの模擬装置では見積もりがずれる)。
        """
        if not (link.status or {}).get("stop_reason"):
            return seen
        got = getattr(link, "run_ended_at", 0.0)
        if not got:
            return seen
        return min(seen, self._after_run_start(link.registered_id, got))

    def _after_run_start(self, dev: str, at: float) -> float:
        """実行の終わりの時刻 at を、開始の記録と最後に書いた周の行より後にする(早送りの
        模擬装置では、経過フレーム数から出した見積もりがずれる)。開始の記録と同じ時刻に
        すると、画面はその実行の終わりとみなさない(前の実行の終わりとみなす。lists.js の
        foldRunLines)。動いたまま未検出から戻った実行は、戻った時の行より後にもする
        (待ちで止まったまま戻った装置の停止した時刻の見積もりは、戻って最初に読めた
        時刻で、その行より前になる。停止させたのは戻った後である)。
        """
        prev = self._lane_lap_head.get(dev)
        floor = max(self._lane_run_at.get(dev, 0.0),
                    prev[2] if prev is not None else 0.0,
                    self._lane_back_at.get(dev, 0.0))
        return max(at, floor + 0.001)

    def _end_run_taken_over(self, gid: str, link, lane: dict) -> None:
        """レーンの実行の後に、装置が別の実行を始めていた(別の手順か、同じ手順を始め
        直した)。レーンの実行の終わりを書く。

        装置の状態はもう別の実行のものなので、停止した状態と時刻は、収集が別の実行を
        読む前に保持したもの(DeviceLink.ended_run)を使う。保持したものがこのレーンの
        実行のものでなければ(収集が停止した状態を一度も読まないうちに次の実行が
        始まった)、止まり方も何周終えたかも分からないとして書く(_same_run)。
        """
        if not self._mark_lane_ended(gid, link.registered_id):
            return
        got = link.ended_run
        if got is not None and _same_run(got, lane):
            status, at = got[1], got[2]
        else:
            status, at = {}, time.time()
        self._note_run_end(link, lane, status=status,
                           at=self._after_run_start(link.registered_id, at))

    def _mark_lane_ended(self, gid: str, dev_id: str) -> bool:
        """そのレーンの終わりを記録済みにする。初めてなら True。"""
        with self._lock:
            g = self._group_by_id(gid)
            if g is None:
                return False
            for x in g.get("lanes") or []:
                if x.get("dev") == dev_id:
                    if x.get("ended"):
                        return False
                    x["ended"] = True
                    # 戻ったら送る止め方は、もう要らない(_keep_stop)
                    x.pop("stop_later", None)
                    self._save_runstate()
                    return True
        return False

    def _note_run_end(self, link, lane: dict, at: float | None = None,
                      status: dict | None = None, why: str = "",
                      group: dict | None = None,
                      stopped: float | None = None,
                      restarted: bool = False) -> None:
        """1 本のレーンの実行の終わりを、何周終えたかとともに残す。

        status を渡すと、いまの装置の状態でなくその状態で見る(始め直しのとき、
        装置はもう次の実行を始めている)。group は、レーンがもう次の実行の
        グループへ移った後に、前の実行の終わりを書くときの前のグループ(のコピー)
        である。連動のもとはそのグループの連動停止で見て、結果はレーンに残さない
        (レーンの記録は次の実行のもので、前の実行の結果を入れると、次の実行の
        結果に見える)。

        装置の記録に残るのは経過フレーム数だけである。何周指定で
        始めたかは開始の記録に、何周終えたかはここに残る。

        終えた周の数は、最後に通った区切りから出す。完了したときの区切りは
        くり返しを抜けた直後にあり、その番号が最後の周にあたる。途中で停止した
        ときの区切りは今の周の中にあるので、終えたのはその 1 つ手前の周までで
        ある。
        """
        st = link.status if status is None else status
        wait = lane.get("wait_end") or {}
        lost = getattr(link, "status_at_loss", None) or {}
        if (not why and wait and _restarted(st) and lapend.held(lost)):
            # 周回を終える予約で止まって待つ間に未検出になり、その間に待つ間が
            # 終わった(人が止め方を押した、ほかのレーンが停止した)。装置は起動し直して
            # 戻った(止まり方も停止した位置も消えている)。未検出になる前の状態と、装置が
            # 停止した時刻で書く(実行が終わったのは待つ間が終わった時で、戻った
            # 時ではない)。開始し直すときに渡される始める前の状態(_take_unended)も同じ
            st = lost
            at = getattr(link, "run_ended_at", 0.0) or at
        lap = self._lap_now(link, lane, st)
        given = why
        why = given or (st or {}).get("stop_reason") or ""
        # 周回の終え方の続きを待つ間の終わり(lapends の drop_of、_failed、
        # _end_rolled_back が置く)。周回を終える予約で止まって待っていた
        # (lapend.held)なら、その終わりで書く。実行が終わったのは、待つのをやめさせた
        # 出来事の時刻である。予約が解除されて動き続け、別の止め方で停止したレーンには
        # 適用しない
        held = not given and bool(wait) and lapend.held(st or {})
        ended_at = (max(at, wait["at"]) if held and at is not None else at)
        if held and wait.get("stop"):
            # 周回を終えるために止まって待つ間に、人が「停止」を押した。押した止め方で
            # 記録する
            why = wait["stop"]
        art = self.project.sent.built(lane.get("proc_id") or "", link.registered_id)
        out = (Project.lap_exit(art.lap_map, (st or {}).get("last_mark"))
               if art is not None else "")
        if out == "last" and why in (proto.STOP_REASON_AT_MARK,
                                     proto.STOP_REASON_STOPPED,
                                     proto.STOP_REASON_BUTTON):
            # 最後の周を実行し終えて、周回数のくり返しを抜けた所で停止した。
            # 後ろに実行するものはないので、完了と同じである(最後の周で「区切りで
            # 停止」を押したときや、連動停止で「いまの周の終わり」に停止させたとき)
            why = proto.STOP_REASON_FINISHED
        if given == proto.STOP_REASON_FINISHED and lap is not None and not out:
            # 周回数を変えて、その周の終わり(次の周の先頭)で終えた。終えたのは
            # 1 つ手前の周までである
            lap -= 1
        if lap is None and lane.get("laps_done") is not None:
            # 周回を終えるために、周回数のくり返しの後ろから続けた実行。変換に周は
            # ないが、周はすべて終えている
            done = lane["laps_done"]
        elif not why:
            # 止まり方がない。実行の途中で装置が起動し直した(電源を入れてから一度も
            # 実行していない装置の STATUS)。最後に通った区切りも消えているので、
            # 何周終えたかは分からない(1 周も終えていないとは言えない)
            done = None
        elif lap is None:
            # 区切りをまだ 1 つも通っていないか、周回数のくり返しより前で停止した。
            # どちらも 1 周も終えていない。分からないのは、送った手順の記録が
            # 読めないときだけである
            done = None if art is None else 0
        elif why == proto.STOP_REASON_FINISHED or out:
            done = lap
        else:
            done = max(0, lap - 1)
        # 周回数のくり返しより前で停止したか。画面は「1 周目の途中」ではなく「周回数の
        # くり返しの前」と表示する(同じ実行の停止した位置は周を持たない言い方で出る)。
        # 区切りを通った後なら、最後に通った区切りがくり返しより前にある。区切りを
        # 1 つも通る前(準備の途中)なら、経過フレーム数が最初の周の頭にまだ達して
        # いない(周の頭も区切りなので、通る前の経過フレーム数に選択待ちの時間は
        # 入らない)。位置から始めた実行の最初の周の頭は、実行の始まり(0)である
        lm = (st or {}).get("last_mark") or {}
        index = lm.get("index")
        heads = _lap_heads(art) if art is not None else {}
        frames = (st or {}).get("frames_elapsed")
        before_laps = (done == 0 and lap is None and art is not None
                       and bool(art.lap_map)
                       and (index < min(item["start"] for item in art.lap_map)
                            if isinstance(index, int)
                            else (bool(heads) and isinstance(frames, int)
                                  and frames < heads[min(heads)])))
        # 周回数のくり返しを抜けた後で停止したか。画面は「N 周目の途中」ではなく
        # 「周回数のくり返しの後ろ」と表示する。周回を終えるために、くり返しの後ろから
        # 続けた実行(laps_done)も、変換に周はないが、くり返しを抜けた後である(周回数で
        # 始めた実行の同じ所と同じに表示する。終えた周の次の周の途中とは表示しない)
        after_laps = ((art is not None and bool(art.lap_map)
                       and positions.past_laps(art.lap_map, index))
                      or (lap is None and lane.get("laps_done") is not None))
        # 停止した区切り。区切りで停止した実行の終わりを、画面は開始位置の一覧の
        # 「停止した位置」と同じ名前で表示する(「3 周目の「連打」」「5 周目の終わり」)。
        # 周の境は、同じレコードにラベルがあっても「3 周目の先頭」と表示する
        # (positions.stop_mark)。名前は送った手順のコピーの区切りから取り出すので、
        # 停止した時点の名前になる
        place = (positions.mark_info(art, index, lm.get("counters"), stop=True)
                 if art is not None else None)
        if place is not None:
            place = {"kind": place["kind"], "name": place["name"],
                     "lap": place["lap"], "after": bool(out)}
        # 区切りの上で停止したか(選択待ちの間に停止した、区切りで停止した)。区切りを
        # 通ってから 1 フレームも進んでいない。選択待ちの間はタイマーが止まって
        # いるので、経過フレームは区切りを通った時点のままである(comm-protocol.md の
        # last_mark)。画面は、区切りで停止と同じく、停止した所を区切りの名前で表示する
        # (開始位置の一覧の「停止した位置」と同じ名前)。選んで先へ進んでから
        # 停止したのは、区切りを過ぎているので「途中」と表示する
        on_mark = (place is not None and isinstance(lm.get("frame"), int)
                   and (st or {}).get("frames_elapsed") == lm["frame"])
        # 周回数は、レーンの記録の周回数で書く。周回の終え方を選んで続きが始まる前に
        # 終わった実行では、元の周回数である(選んだ周回数で動くのは続きからで、続きの
        # レーンの記録がその周回数を持つ)
        laps = lane.get("laps")
        # 未検出の間に起動し直した装置の終わり(restarted。lapends の _end_restarted)
        # は、未検出になる前の状態で停止した所と周を書くが、止まり方は分からない
        # (区切りで停止とは書かない)
        shown = "" if restarted else why
        e = {"a": (0 if done is None else done), "name": lane.get("proc") or "",
             "done": done, "laps": laps, "why": shown}
        if on_mark:
            e["on_mark"] = True
        if lane.get("nolaps"):
            e["nolaps"] = True
        if before_laps:
            e["before_laps"] = True
        if after_laps:
            e["after_laps"] = True
        if place is not None:
            e["place"] = place
        if ended_at is not None:
            e["at"] = ended_at
        if held and at is not None and at < ended_at:
            # 待つのをやめさせた出来事の時刻で並べる。最後の周の長さは、装置がその周を
            # 進んだ長さで示すので、装置が停止した時刻も残す(画面の周の行の時間。
            # lists.js の lapSpan)。待った間を含めると「1 周目 31 秒」と出る
            e["stopped_at"] = at
        elif stopped is not None and stopped < (
                ended_at if ended_at is not None else time.time()):
            # 周回の終え方の続きを始めたときに、周の境で止まって待っていたレーンが
            # 後ろに何もなく終えた(lapends の _restart_with)。行は続きを始めた時刻で
            # 書き、周の長さは装置が周の境で停止した時刻までとする(上と同じ)
            e["stopped_at"] = stopped
        # 周回の終え方の続きを始められずに停止した(lapends の _failed)。装置は予約で
        # 区切りの上に停止したので、止まり方は区切りで停止のままである。記録とレーンの
        # メッセージは「周回の終え方を変更できずに停止」と書く(誰も押していない
        # 「区切りで停止」に読めない)。完了したなら完了と書く
        failed = held and bool(wait.get("failed")) and why != proto.STOP_REASON_FINISHED
        if failed:
            e["lap_end_failed"] = True
        # ほかのレーンに連動して停止したなら、もとのレーンとその止まり方も残す
        # (記録とレーンのメッセージは「1P の完了に連動」と書く。自分の止まり方の
        # 「区切りで停止」では、誰も押していない停止に読める)
        cause, reason = (("", "") if failed else self._linked_cause(
            link.registered_id, why, group, stopped=(st or {}).get("stopped_mask")))
        held_by = wait.get("by") if held else None
        if not cause and not failed and held_by:
            # 周回を終えるために止まって待つ間に、まだ動いていたレーンが予約と違う
            # 止まり方で停止した(lapends の drop_of の cause)。そのレーンに連動したと
            # 書く
            cause, reason = held_by[0], held_by[1]
        if cause:
            e["linked_by"] = cause
            e["linked_reason"] = reason
        # 停止した文が周を示す(「3 周目の途中」「3 周目の「連打」」)なら、その周の行を
        # 先に書いていなければ書く。周の頭の区切りの上で停止した(「3 周目の先頭」)のは、
        # その周を 1 フレームも動いていないので、前の周の行に置く。周回数のくり返しを
        # 抜けた所で停止した実行と、周回数を変えて周の境で終えた実行は、最後の周の
        # 次の周の境で停止している。どちらも、最後の周までに抜けた周の行は書く(終わりの
        # 行が前の周の行に付かない)。周回数のくり返しの前と、抜けた所より後ろと、
        # 何周目か分からない終わりは書かない
        on_head = ((why == proto.STOP_REASON_AT_MARK or on_mark)
                   and (place or {}).get("kind") == "lap")
        past = bool(out) or given == proto.STOP_REASON_FINISHED
        if (lap is not None and done is not None
                and not (before_laps or (after_laps and not out)
                         or lane.get("nolaps"))):
            entered = not (past or on_head)
            self._note_missed_lap(
                link, done + 1 if past else lap, st, at, own=status is None,
                entered=entered,
                head=self._lap_head_frame(link, lane, st or {}) if entered else None)
        # 開始し直すときに前の実行の終わりを書く(group がある)ときも付ける。この後に
        # 開始が保持した値を捨てる(_record_group)
        held_s = self._lane_held.pop(link.registered_id, None)
        if held_s:
            # 周回の終え方の続きで終えた。続きを始めるまで止まって待った長さを
            # 付け、最後の周の時間から除く(_log_lap と同じ)
            e["held_s"] = round(held_s, 3)
        self._log(link, "PC_RUN_END", **e)
        if group is not None:
            return
        # 前回の結果としてレーンに残す。画面はレーンにこれを出し、タブの名前の
        # 「完了」もここから作る。次の開始で、新しいグループのレーンに替わる。
        # 停止した区切り(mark)は、周回を終えて途中の区切りで完了したレーンに、
        # 停止した位置を出さないために持つ(guistate)
        self._set_lane_result(link.registered_id, {
            "why": shown, "done": done, "laps": laps,
            "nolaps": bool(lane.get("nolaps")), "before_laps": before_laps,
            "after_laps": after_laps, "place": place, "on_mark": on_mark,
            "proc": lane.get("proc") or "",
            "at": ended_at if ended_at is not None else time.time(),
            "mark": [lm.get("index"), list(lm.get("counters") or [])],
            "linked_by": cause, "linked_reason": reason, "lap_end_failed": failed,
            # 人がそのレーンを指して押した止め方で停止したか(_by_hand)。グループの完了の
            # 通知とタブの「完了」は、このレーンを数えずに決める(notify の _outcome)
            "by_hand": self._by_hand(lane, why) and not failed})

    def _note_missed_lap(self, link, lap: int, status: dict | None,
                         end_at: float | None, own: bool, entered: bool,
                         head: int | None = None) -> None:
        """監視が読む前に入った周の行(PC_LAP)を、実行の終わりの前に書く。

        周の行は、監視が動いている装置の周の境を読んだときに書く。周の境の
        すぐ後で停止すると、読む前に停止して書かれず、停止した文が前の周の行に付く。
        lap は停止した状態の最後に通った区切りがある周で、entered が偽なら、その区切りは
        lap 周目の境そのもので、lap 周目には入っていない(_note_laps)。head は lap 周目の
        頭の経過フレーム数である(_lap_head_frame)。

        時刻は、停止した状態の最後に通った区切りを通った時刻から、その周の頭までの
        フレーム数を引いた時刻である(_mark_time、_note_laps)。停止したときの装置の
        行は、この行より先に書かれているが、記録を 1 行 1 周にまとめるときに、この時刻
        より後の行はこの周の行に入る(RunLog.group_by_lap)。
        """
        st = status or {}
        self._note_laps(link, lap, st, self._mark_time(link, st, end_at, own),
                        entered=entered, head=head)

    def _mark_time(self, link, st: dict, end_at: float | None, own: bool) -> float:
        """状態 st で最後に通った区切りを、装置が通った時刻(UNIX 秒)。

        end_at は状態 st を見た時刻で、None ならいまである。そこから、区切りを通った
        後の経過フレーム数のぶんを引く。own(st がいまの装置の状態)なら、収集が状態を
        読んだときに経過フレーム数から出した、その区切りを通った時刻(DeviceLink.passed_at)
        を使う。見た時刻から引くだけでは、読んでから見るまでの間のぶん遅れる。
        停止した後は、装置の行は収集が停止した状態を読んだ周期に書くので、その時刻は
        監視が初めて停止を見た時刻(終わりの時刻)より前になりうる。終わりの時刻から
        引いただけでは装置の行より後になり、行が前の周に残る。
        """
        lm = st.get("last_mark") or {}
        at = time.time() if end_at is None else end_at
        if (isinstance(st.get("frames_elapsed"), int)
                and isinstance(lm.get("frame"), int)):
            period = (link.info.frame_period_ns if link.info
                      else proto.DEFAULT_FRAME_PERIOD_NS)
            at -= max(0, st["frames_elapsed"] - lm["frame"]) * period / 1e9
        passed = link.passed_at(lm) if own else 0.0
        if passed:
            at = min(at, passed)
        return at

    def _note_laps(self, link, lap: int, st: dict, at: float,
                   entered: bool = True, head: int | None = None) -> None:
        """lap 周目に入った行(PC_LAP)を書く。前に書いた周との間に抜けた周があれば、
        先にその周の行も書く。

        st は lap 周目で最後に通った区切りを見た状態、at はその区切りを装置が通った
        時刻である。区切りは普通は周の境だが、監視がその周を初めて読む前に、周の中の
        次の区切りを通っていれば、その区切りである。entered が偽なら、区切りは
        lap 周目の境そのもので、lap 周目には入っていない(その周の行は書かず、
        抜けた周だけを書く)。head は lap 周目の頭を通ったときの経過フレーム数
        (_lap_head_frame。分からなければ None)である。

        区切りが周の途中なら、周の行の時刻は、区切りを通った時刻から、周の頭からその
        区切りまでのフレーム数を引いた時刻にする。区切りを通った時刻にすると、その周の
        行が遅れ、抜けた周が等しく引き伸ばされる(1 周 3 秒の周が「4 秒」「4 秒」
        「1 秒」と出る)。行に周の番号を含める。サーバーを起動し直すと、どの周まで
        書いたかを保持した値(_lane_lap など)が消えて同じ周を二度書きうるが、番号で
        まとめれば重複は害にならない。

        監視が読む間に 2 周以上進む(1 周が 1 秒より短い、未検出の間に周が進んだ)と、
        間の周の行が抜ける。抜けた周の境は、前に書いた周の境と今の周の頭の経過フレーム数
        を、周の数で等分して出す(同じ長さの周なら正しい)。経過フレーム数は RUN ごとに
        数え直すので、前に書いた周が別の RUN(周回を終えるための続き)なら、時刻を
        等分する。開始の後にまだ 1 行も書いていなければ、開始のときに分かった開始の周
        から書く(_heads_from)。サーバーを起動し直した後は、どの周まで書いたか分からない
        ので、前の周を書かない。

        装置が到達しえない周は書かない(安全策)。周は 1 周目から数え、1 周は少なくとも
        1 フレームかかるので、前に書いた周(まだなければ開始)から今までに通る周の境の
        数は、その間の時間とフレーム数から分かる数を超えない(_reach)。超えたら、
        間の周も今の周も書かず、前に書いた周も変えない。到達しえない周は、レーンの手順と
        違う実行の区切りを読んだときに出る(周回数が無限の手順の対応表で別の手順の
        カウンターを読むと 4294967294 周目になる)。前に書いた周はこの確かめを通って
        いるので、疑わしいのは今の周である。今の周の行を書くと記録に偽りの周が残り、
        前に書いた周にすると、次の正しい周をそれと比べることになる(負の周なら、次の
        正しい周までの 40 億行あまりを書くことになる)。理由は操作画面のサーバーの出力に
        同じ実行で 1 回出す(_skip_laps)。
        """
        dev = link.registered_id
        last = self._lane_lap.get(dev)
        prev = self._lane_lap_head.get(dev)
        if lap < 1:
            self._skip_laps(link, f"{lap} 周目と読みました",
                            "周は 1 周目から数えるため、ログに周を記録しません")
            return
        # 装置は RUN を受けてから始め、周は順に進むので、開始の記録の時刻と前に書いた
        # 周の時刻より前の時刻にはならない。経過フレーム数を実時間より速く進める
        # 模擬装置(早送り)では、フレーム数から出した時刻がそれより前になるので、
        # 揃える(記録の並びが崩れない)
        floor = self._lane_run_at.get(dev, 0.0)
        at = max(at, floor,
                 prev[2] if prev is not None and prev[0] == last else 0.0)
        frame = (st.get("last_mark") or {}).get("frame")
        proc = st.get("proc") or ""
        period = (link.info.frame_period_ns if link.info
                  else proto.DEFAULT_FRAME_PERIOD_NS)
        # 今の周の頭の経過フレーム数(hf)と時刻(at_head)。読んだ区切りが周の頭なら、
        # 区切りと同じである
        if (entered and isinstance(head, int) and isinstance(frame, int)
                and head <= frame):
            hf = head
            at_head = max(at - (frame - head) * period / 1e9, floor,
                          prev[2] if prev is not None and prev[0] == last else 0.0)
        else:
            hf, at_head = frame, at
        start = self._lane_lap_from.get(dev) if last is None else None
        heads = (self._heads_from(start[1], start[2], lap, frame,
                                  lap if entered else lap - 1, head=hf)
                 if start is not None and start[0] == proc and start[1] is not None
                 and isinstance(frame, int) else None)
        if heads is not None:
            reach = self._reach(link, floor, frame)
            if lap - start[1] + 1 > reach:
                self._skip_laps(
                    link, f"開始の {time.time() - floor:.1f} 秒後に {lap} 周目と"
                          "読みました",
                    f"その間に進めるのは {reach} 周までのため、ログに周を記録しません")
                return
        if last is None:
            self._lane_lap_from.pop(dev, None)
        if heads is not None:
            # 時刻は、今の区切りを通った時刻から、そこまでの経過フレーム数を引いて出す
            for k, fk in heads:
                floor = min(at, max(floor, at - (frame - fk) * period / 1e9))
                self._lane_lap[dev] = k
                self._lane_lap_head[dev] = (k, round(fk), floor, proc)
                self._log_lap(link, k, floor)
            return
        if last is not None and lap - last >= 2 and prev is not None \
                and prev[0] == last:
            _lap, f0, t0, proc0 = prev
            same = (proc0 == proc and isinstance(f0, int) and isinstance(hf, int)
                    and hf >= f0)
            reach = self._reach(link, t0, (frame - f0) if same
                                else (frame if isinstance(frame, int) else 0))
            if lap - last > reach:
                self._skip_laps(
                    link, f"{last} 周目の {time.time() - t0:.1f} 秒後に {lap} 周目と"
                          "読みました",
                    f"その間に進めるのは {reach} 周までのため、ログに周を記録しません")
                return
            for k in range(last + 1, lap):
                part = (k - last) / (lap - last)
                if same:
                    fk = f0 + (hf - f0) * part
                    t = max(t0, at_head - (hf - fk) * period / 1e9)
                    self._lane_lap_head[dev] = (k, round(fk), t, proc)
                else:
                    t = t0 + (at_head - t0) * part
                    self._lane_lap_head[dev] = (k, None, t, proc)
                self._lane_lap[dev] = k
                self._log_lap(link, k, t)
        if entered and lap != self._lane_lap.get(dev):
            self._lane_lap[dev] = lap
            self._lane_lap_head[dev] = (lap, hf, at_head, proc)
            self._log_lap(link, lap, at_head)

    def _log_lap(self, link, lap: int, at: float) -> None:
        """lap 周目に入った行(PC_LAP)を、時刻 at で書く。

        周回の終え方の続きを待って止まっていたレーンの、続きを始めてから最初の周の行には、
        待った長さ(held_s。秒)を付ける。その長さは、この行で閉じる前の周の時間に
        入っているが、装置はその周を進んでいない。画面は周の時間からそれを除く
        (lists.js の lapSpan)。
        """
        held = self._lane_held.pop(link.registered_id, None)
        self._log(link, "PC_LAP", a=lap, at=at,
                  **({"held_s": round(held, 3)} if held else {}))

    def _lap_head_frame(self, link, lane: dict, st: dict) -> int | None:
        """状態 st の最後に通った区切りがある周の、頭を通ったときの経過フレーム数。
        分からなければ None。

        区切りが周の頭からどれだけ後ろにあるかは、送った手順のコピーの区切りの表で
        分かる。周回数のくり返しの対応表の項目(同じ形の周が続く範囲)の最初の周の頭と、
        その区切りの経過フレーム数の差で、項目のどの周でも同じである。位置から始めた
        実行の最初の項目は、開始の周の途中から始まるので、実行の始まり(0)を頭とする
        (_lap_heads と同じ)。区切りの表は選択肢の長さを 0 として数えるので、その間に
        選択肢を通っていれば、差は小さく出る(頭は遅めに出る。区切りより後にはならない)。
        """
        lm = st.get("last_mark") or {}
        index, frame = lm.get("index"), lm.get("frame")
        art = self.project.sent.built(lane.get("proc_id") or "", link.registered_id)
        if art is None or not isinstance(index, int) or not isinstance(frame, int):
            return None
        for item in art.lap_map or []:
            if item.get("exit") or not item["start"] <= index < item["end"]:
                continue
            here = (Project.mark_at(art.marks, index) or {}).get("frame")
            top = (0 if item["start"] == 0
                   else (Project.mark_at(art.marks, item["start"]) or {}).get("frame"))
            if not isinstance(here, int) or not isinstance(top, int):
                return None
            return max(0, frame - max(0, here - top))
        return None

    @staticmethod
    def _reach(link, since: float, frames: int) -> int:
        """時刻 since(UNIX 秒)から今までに、装置が通りうる周の境の数の上限。

        1 周は少なくとも 1 フレームかかる。選択待ちの間は装置の経過フレーム数が進まない
        ので、選択待ちだけの周は経過フレーム数では数えられないが、実時間は 1 フレーム
        以上かかる。そこで、その間の実時間を 1 フレームの長さで割った数に、装置が
        数えたその間の経過フレーム数 frames を足す。早送りの模擬装置は、1 フレームの
        長さを実機と同じ値で返しながら実時間より速く進むので、実時間だけでは足りない。
        実機では同じ時間を 2 度数えるので緩くなるが、上限であることは変わらない。
        """
        period = (link.info.frame_period_ns if link.info
                  else proto.DEFAULT_FRAME_PERIOD_NS)
        return (int(max(0.0, time.time() - since) * 1e9 / period)
                + max(0, frames) + 1)

    def _skip_laps(self, link, *parts: str) -> None:
        """周の行を書かなかった理由を、操作画面のサーバーの出力に出す。同じ実行では
        1 回だけ出す(到達しえない周は、同じ状態を読むたびに出る)。"""
        dev = link.registered_id
        if dev in self._laps_skipped:
            return
        self._laps_skipped.add(dev)
        name = link.cfg.get("name") or dev
        print(sentences(f"{name}: {parts[0]}", *parts[1:]), file=sys.stderr,
              flush=True)

    @staticmethod
    def _heads_from(first: int, heads: dict, lap: int, frame: int,
                    upto: int, head: int | None = None) -> list | None:
        """開始の周 first から upto 周目までの、周の頭の経過フレーム数。
        [(周, 経過フレーム数)] を返す。開始の周の頭が分からなければ None。

        heads は変換の結果から分かる周の頭(_lap_heads)、lap と frame は今の区切りが
        ある周と、それを通った経過フレーム数、head は今の周の頭の経過フレーム数
        (_lap_head_frame)である。頭の分からない周は、前後の分かる周(今の周を含む)の
        間を等分する。今の周の頭も分からなければ、今の区切りをその周の位置とする(周の
        行の時刻は、今の区切りを通った時刻になる)。区切りの表の
        経過フレーム数は選択肢の長さを 0 として数えるので、その前に選択肢を通って
        いれば、装置が数えた経過フレーム数より小さい(頭の時刻は早めに出る)。今の
        区切りより後にはしない。
        """
        known = {k: min(f, frame) for k, f in heads.items() if first <= k <= lap}
        if first not in known:
            return None
        known.setdefault(lap, frame if head is None else min(head, frame))
        out = []
        lo = first
        for k in range(first, upto + 1):
            if k in known:
                lo = k
                out.append((k, known[k]))
                continue
            hi = min(j for j in known if j > k)
            out.append((k, known[lo] + (known[hi] - known[lo]) * (k - lo) / (hi - lo)))
        return out

    def _linked_cause(self, dev_id: str, why: str, group: dict | None = None,
                      stopped: int | None = None) -> tuple[str, str]:
        """そのレーンがほかのレーンに連動して停止したなら、もとのレーンの装置 ID と
        その止まり方(stop_reason)。連動でなければ ("", "")。

        group を渡すとそのグループの連動停止で見る。渡さなければ、そのレーンが
        いま属するグループで見る。完了したレーンは連動ではない(停止させるより先に
        終えている)。連動停止を受け取っていないレーン(未検出のまま戻らなかった、送れ
        なかった)は、自分の止まり方で記録する。

        stopped は、停止したときの装置の stopped_mask(停止させた予約)である。周の
        終わりの予約は周の境だけなので、停止させた予約がそれでなければ、予約は停止する
        前に人が上書きしていた(装置の上面のボタンの短押しか、別のツールからの
        「区切りで停止」)。
        監視が上書きを読む前に停止したときも、これで分かる(読めたときは _clear_pending
        が cancelled に入れる)。
        """
        if not dev_id or why == proto.STOP_REASON_FINISHED:
            return "", ""
        if group is None:
            with self._lock:
                group = next(
                    (json.loads(json.dumps(g)) for g in self._state.get("groups") or []
                     if any(x.get("dev") == dev_id for x in g.get("lanes") or [])),
                    None)
        ls = (group or {}).get("linked_stop") or {}
        # 連動停止を置き換えたグループは、置き換えた前の連動停止を replaced に古い順に
        # 持つ(_linked_stop の over)。そのレーンへ送れた中で一番新しいもので見る。
        # 新しい方を受け取る前に、前の予約で停止したレーンは、前の連動停止で見る
        for one in (ls, *reversed(ls.get("replaced") or [])):
            cause = one.get("cause") or ""
            if not cause or cause == dev_id or dev_id not in (one.get("sent") or []):
                continue
            # 区切りや周の終わりで停止する予約は、その止まり方(区切りで
            # 停止した)で停止したときだけ連動である。予約の間に人が停止させた・
            # 予約を取り消した・人の「区切りで停止」で上書きした(どちらも cancelled。
            # _take_over、_clear_pending)レーンは、自分の止まり方で記録する
            mode = _LINKED_MODE.get(one.get("reason") or "", "immediate")
            if mode != "immediate" and (
                    why != proto.STOP_REASON_AT_MARK
                    or dev_id in (one.get("cancelled") or [])
                    or (mode == "lap" and stopped != binfmt.MARK_LAP)):
                return "", ""
            return cause, one.get("reason") or ""
        return "", ""

    def _set_lane_result(self, dev_id: str, result: dict) -> None:
        """そのレーンの最後の実行の結果を、属するグループの記録に書く。

        result は連動のもと(linked_by・linked_reason)も持つ(_linked_cause)。
        """
        if not dev_id:
            return
        with self._lock:
            for g in self._state.get("groups") or []:
                for x in g.get("lanes") or []:
                    if x.get("dev") != dev_id:
                        continue
                    x["result"] = dict(result)
                    self._save_runstate()
                    return

    def _finish_group(self, gid: str) -> None:
        """1 回の実行の終わりを記録する(グループは消えない)。"""
        with self._lock:
            cur = self._group_by_id(gid)
            if cur is None or cur.get("ended_at") is not None:
                return                    # 二重には閉じない
            cur["ended_at"] = _now()
            self._save_runstate()

    # ---- ログ(PC 側の合成。装置タグ付きで logs.jsonl へ) ----

    def _log(self, link, kind: str, **fields) -> None:
        e = {"kind": kind, **fields}
        dev_id = link.registered_id or ""
        self.project.logs.append([e], dev=dev_id)
