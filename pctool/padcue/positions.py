"""実行の位置(止まる先、停止した位置、開始位置)を求める。

開始の指示を開始位置の形に揃え、止まる先と停止した位置を、装置の状態と送った
手順のコピーから求める。装置は手順の名前もブロックの識別子も持たず、レコード番号と、
最後に通った区切りと、くり返しの回数だけを返す。画面に出す位置は、送った手順の
コピー(Project.sent の built・view・records)と照合して作る。
"""
from __future__ import annotations

import json
import threading
import traceback
from dataclasses import dataclass

from . import binfmt, procmodel, timeline
from .binfmt import Await
from .engine import WalkError, after_arms, walk
from .lapplan import LapsLeft
from .project import Project
from .sent import SentView

# 送った手順のコピーが読めない形のとき、コピーから求める所が出す例外(項目の数や型が
# 違う)。受けるのは画面の状態を作る所で、そのレーンかグループの、コピーから求める
# 欄だけを出さない(guistate、lapends)。状態の取得が失敗すると、画面全体が使えなく
# なり、そのレーンを停止する操作もできない
BAD_COPY = (ValueError, TypeError, LookupError)
# 読めない理由をサーバーの出力に出した実行(鍵 -> 実行。tell_bad_copy)
_BAD_COPY_TOLD: dict = {}


def tell_bad_copy(key, run) -> None:
    """送った手順のコピーが読めない理由(traceback)を、操作画面のサーバーの出力に出す
    (BAD_COPY を受けた except の中から呼ぶ)。

    画面には理由を出さないが、出力に残さないと、たまにしか起きない失敗の原因を後から
    追えない。状態は毎秒(開いているタブごとに)作るので、同じ鍵(装置 ID など)の同じ
    実行(run)については 1 回だけ出し、実行が替われば、また 1 回出す。
    """
    if _BAD_COPY_TOLD.get(key) != run:
        _BAD_COPY_TOLD[key] = run
        traceback.print_exc()


def _is_int(v) -> bool:
    """真偽値を除く整数か(JSON の true は Python では 1 と等しい)。"""
    return isinstance(v, int) and not isinstance(v, bool)


def start_item(p: dict) -> dict:
    """開始の指示 1 レーンぶんを、グループの開始が受け取る形に揃える。

    レーンの開始も一括操作の開始も、この形の並びとして 1 つの経路を通る。
    周回数は合計で、欄がないか null なら無限である。
    開始位置は、変換のときに作った開始位置の一覧の項目をそのまま受け取る。
    欄がなければ先頭である。名前ではなくブロックの識別子で指すのは、同じ名前の
    ラベルが呼び出し階層の中に複数あり得るからである。
    """
    at = p.get("start") or {}
    raw = p.get("laps")
    # 画面は整数しか送らないが、コマンドやほかのツールの要求も同じ経路を通る。
    # 小数を切り捨てたり、真偽値を 1 と読んだりせず、整数か整数の文字列だけを受ける
    if raw in (None, ""):
        laps = None
    elif _is_int(raw) or (isinstance(raw, str) and raw.strip().isdigit()):
        laps = int(raw)
    else:
        raise ValueError(procmodel.LAPS_BAD)
    turns = list(at.get("turns") or [])
    if not all(_is_int(x) for x in turns):
        raise ValueError("開始位置が正しくありません。開始位置を選択し直してください。")
    # 先頭かどうかはブロックの識別子だけでは決まらない。手順全体を囲むくり返しの
    # 周の境で停止した位置は、ブロックを持たず、何周目かだけを持つ(procmodel.StartPos)
    head = (not at.get("block") and not at.get("path")
            and all(k == 1 for k in turns))
    start = None if head else {"name": str(at.get("name") or ""),
                               "path": [str(x) for x in at.get("path") or []],
                               "block": str(at.get("block") or ""),
                               "turns": turns}
    # ブロックを終えた直後を指す位置(最後の周の終わりで停止した位置)
    if start is not None and at.get("after"):
        start["after"] = True
    # 停止した位置は、回番号がどのくり返しの回かも持つ(start_from_mark)。
    # 開始の前に、今の手順でも同じくり返しかを照らすために渡す
    if start is not None and isinstance(at.get("loops"), list):
        start["loops"] = [str(x) for x in at["loops"]]
    return {
        "dev": str(p.get("dev", "")),
        "name": str(p.get("name", "")),
        "laps": laps,
        "start": start,
    }


# 止まる先を探すとき、マスクに当てはまらない区切りをいくつまで通り過ぎるか。
# 無限のくり返しで 1 つも当てはまらない手順をたどり続けないための上限である
_STOP_TARGET_MARKS = 1000


def mark_info(art, index, counters=None, *, stop: bool = False) -> dict | None:
    """レコード番号を、利用者に見せる区切りの形に変換する。

    返すのは {"index", "lap", "kind", "name"} である。周は対応表から出す。
    表にないレコードなら None を返す。周回数のくり返しの後ろの区切り(抜けた所に
    あるラベルを含む)は、どの周の一部でもないので周を持たない(lap が None)。
    抜けた所の周の境だけは、最後の周の終わりとして周を持つ(「5 周目の終わり」)。
    stop は、停止した所として探すか(stop_mark。停止した位置と実行の終わりの記録)。
    """
    m = stop_mark(art, index) if stop else Project.mark_at(art.marks, index)
    if m is None:
        return None
    kind = m.get("kind", "")
    lap = (None if kind != "lap" and past_laps(art.lap_map, index) else
           Project.lap_of(art.lap_map,
                          {"index": index, "counters": list(counters or [])}))
    return {"index": index, "lap": lap, "kind": kind, "name": m.get("name", "")}


def turns_of(loop_map: list, mark: dict, counters) -> list[int] | None:
    """区切りを囲むくり返しのそれぞれが、何回目かを出す(外側から)。

    区切りの loop_refs が指す対応表の項目と、装置が返したその時点の各
    カウンターの値から出す(dsl.Compiled.loop_map の式)。特定できなければ None。
    """
    loops = list(mark.get("loops") or [])
    refs = list(mark.get("loop_refs") or [])
    if len(refs) != len(loops):
        return None
    values = list(counters or [])
    out = []
    for ref in refs:
        if not (isinstance(ref, int) and 0 <= ref < len(loop_map or [])):
            return None
        item = loop_map[ref]
        n = item["first"]
        for c in item.get("counters") or []:
            num = c["counter"]
            left = values[num] if num < len(values) else 0
            n += (c["total"] - left) * c["step"]
        out.append(n)
    return out


def start_from_mark(mark: dict, lap, turns) -> dict | None:
    """その区切りから始めるための開始位置(file-format.md §6-2)。

    作れないときは None を返す。回番号は、位置を囲むくり返しの数だけ必要である。
    周の境は lap(何周目か)を、ほかの区切りは turns(turns_of が出した、囲む
    くり返しのそれぞれが何回目か)を使う。選択待ち(分岐・合流点・スクリプト)の
    位置は、ラベルと同じに扱う。そこから始めると、その選択待ちからやり直す。
    """
    loops = list(mark.get("loops") or [])
    kind = mark.get("kind", "")
    if kind not in ("lap", "label", "branch", "join", "script"):
        return None
    if kind == "lap":
        # 周の境は、そのくり返し自身の回を指す。周回数のくり返しは一番外側に
        # あるので、さらに外のくり返しはない
        if loops or lap is None:
            return None
        turns, owners = [lap], [mark.get("block", "")]
    else:
        if turns is None:
            return None
        turns, owners = list(turns), loops
    # loops は、turns の回番号がどのくり返しの回かを並べたもの(区切りの loops と
    # 同じ形で、手順全体のくり返しは "")。停止した後に手順を修正してくり返しの形が
    # 変わったら、Project.start_loops_match がここと比べて始め直さない
    return {"path": list(mark.get("path") or []),
            "block": mark.get("block", ""), "turns": turns, "loops": owners}


def stop_mark(art, index) -> dict | None:
    """停止した区切り。周の境は、同じレコードにラベルがあっても周の境として返す。

    周の先頭のレコードには、周の先頭に置いたラベルや、元の周回数でその周に選ばれた
    選択肢の中のラベルもある。そこで停止したのは周の境で、その周をまだ 1 フレームも
    動いていない。ラベルとして扱うと、停止した位置から周回数を変えて始め直したとき、
    その周は元の周回数で選ばれていた選択肢を通る。周の先頭から始め、その周の選択肢を
    新しい周回数で選び直す(最初からその周回数で始めた実行と同じ動き。周回の終え方の
    続きも同じ。lapends の _restart_with)。周回数のくり返しを抜けた所は、後ろの
    ラベルで呼ぶ(周を持たない位置で、始める位置は stopped_at が別に作る)。

    周の境でないレコードにラベルが 2 つ以上あるのは、間のブロックがその回に何も
    出さなかったとき(最後の回でない周の「最後の回」の分岐など)である。装置はその
    レコードを通ったとしか返さないが、停止したのはそのすべてを過ぎた後なので、最後に
    書いてあるラベルを返す。前のラベルを返すと、始め直すときに間のブロックをもう一度
    通り、周回数を変えて始めると、元の実行が通らなかった選択肢を通りうる。区切りの表は
    レコード番号の順で、同じレコードの中は書いてある順である(dsl.compile_procs)。
    ラベルが 1 つ以下のレコード(1 台の変換で省いた合流点と重なるものを含む)は、
    Project.mark_at で探す。
    """
    lap = lap_mark_at(art.marks, index)
    if lap is not None and not Project.lap_exit(art.lap_map, {"index": index}):
        return lap
    labels = [m for m in art.marks or []
              if m.get("index") == index and m.get("kind") == "label"]
    return labels[-1] if len(labels) >= 2 else Project.mark_at(art.marks, index)


def lap_mark_at(marks, index) -> dict | None:
    """そのレコードの周の境の区切り。なければ None。

    周の先頭と、周回数のくり返しを抜けた所には、ラベルも同じレコードにありうる
    (Project.mark_at は名前を持つ区切り、つまりラベルを先に返す)。周の境として
    扱う所は、これで探す。
    """
    return next((m for m in marks or []
                 if m.get("index") == index and m.get("kind") == "lap"), None)


def stopped_at(art, st: dict) -> dict | None:
    """停止した位置。動いている間と、完了したときは出さない。

    完了で出さないのは、そこが「途中で停止した位置」ではないからである。
    そこから始め直せる位置なら、開始位置そのものも付ける。周の境で停止したときは、
    同じレコードにラベルがあっても周の先頭である(stop_mark)。手順全体の 1 周目の
    先頭と、手順の先頭から始まる周回数のくり返しの 1 周目の先頭で停止したときは、
    開始位置の「先頭」と同じ所なので head を付ける(画面は「始められない位置」と
    表示せずに先頭を選ぶ)。

    最後の周の終わり(周回数のくり返しを抜けた所)で停止したときは、
    周回数のくり返しの直後から始める位置にする(after)。周の先頭から始めると、
    終えた周をもう一度実行する。uses_laps は、そこから始めたときに周回数が反映されるか
    で、周回数のくり返しより後ろで停止した位置では偽になる(開始位置の一覧の
    項目の uses_laps と同じ意味。画面は周回数の欄を閉じる)。
    """
    if st.get("running") or st.get("awaiting"):
        return None
    if (st.get("stop_reason") or "") in ("", "finished"):
        return None
    lm = st.get("last_mark") or {}
    out = Project.lap_exit(art.lap_map, lm)
    if out == "last":
        return None      # すべての周を実行し終えた所。途中で停止した位置ではない
    got = mark_info(art, lm.get("index"), lm.get("counters"), stop=True)
    if got is None:
        return None
    mark = stop_mark(art, lm.get("index")) or {}
    got["proc"] = art.name           # 画面はこの手順が選ばれている間だけ出す
    got["uses_laps"] = not past_laps(art.lap_map, lm.get("index"))
    if out:
        # 抜けた所の直後にラベルがあっても、指すのは周回数のくり返しである
        lap_mark = lap_mark_at(art.marks, lm.get("index")) or mark
        start = {"path": list(lap_mark.get("path") or []),
                 "block": lap_mark.get("block", ""), "turns": [], "after": True}
    else:
        start = start_from_mark(
            mark, got.get("lap"),
            turns_of(art.loop_map, mark, lm.get("counters")))
    # 手順全体の 1 周目の先頭と、手順の先頭から始まる周回数のくり返しの 1 周目の
    # 先頭は、開始位置の「先頭」と同じ所である
    if (start and not start["path"] and all(k == 1 for k in start["turns"])
            and (not start["block"] or _same_as_the_head(art, mark))):
        start = None
        got["head"] = True
    got["start"] = start
    return got


def _same_as_the_head(art, mark: dict) -> bool:
    """区切り mark(周の境)が、手順の先頭と同じ所か。

    周回数のくり返しの前に、時間の進むブロックも、ラベルのほかの区切り(選択待ちや
    合流点)もなければ、1 周目の先頭から始めるのは先頭から始めるのと同じ動きである。
    区切りのフレーム数は実行の始まりから数えるので、位置から始めた実行(art.start)の
    区切りは手順の先頭と比べられない。準備のある手順をその 1 周目の先頭から始めた
    実行でも、1 周目の先頭は送った手順の頭(0 フレーム)にあるが、先頭から始めると
    準備をもう一度通る。開始位置を持たない変換の結果(dsl.Compiled)は、先頭から
    始めたかが分からないので、同じ所とはみなさない。
    """
    if (not hasattr(art, "start") or art.start is not None
            or mark.get("kind") != "lap"):
        return False
    if mark.get("frame") != 0 or not isinstance(mark.get("index"), int):
        return False
    return all(m.get("kind") == "label" for m in art.marks or []
               if isinstance(m.get("index"), int) and m["index"] < mark["index"])


def past_laps(lap_map: list, index) -> bool:
    """そのレコードが、周回数のくり返しより後ろにあるか。

    くり返しを抜けた所の項目(exit)より後ろのレコードがそうである。周回数の
    くり返しの後ろから始めた実行は、くり返しを変換しないので対応表が空で、
    どのレコードも後ろにある。手順全体を周回数のくり返しで囲む手順の抜けた所は
    手順の終わり(完了)である。
    """
    if not lap_map:
        return True
    return isinstance(index, int) and any(
        item.get("exit") and item["start"] <= index for item in lap_map)


def still_startable(project, stopped: dict | None) -> dict | None:
    """停止した位置が、今の手順でも始められる位置かを確認する。

    始められなければ開始位置(start)を外し、lost を付ける(停止した後に手順を
    修正して位置がなくなった。画面は、始め直せない位置で停止したときと書き分ける)。
    回番号が停止したときと別のくり返しを指すようになった位置も、始められない
    位置とする(変換は通っても、別の回から始まる)。
    位置そのもの(何周目のどこか)は残す。変換は送った手順のコピーを使うので、停止して
    いる間の毎秒の確認は軽い。今の手順が 2 周目へ進まない手順なら、uses_laps を偽に
    する(周回数の欄を閉じる。開始位置の一覧の項目と同じ)。図の上の開始の線の位置は
    ここでは出さない。待機中の図は周回数の欄の値で描くので、画面が図を取得するときに
    いっしょに求める(gui の /api/timeline の at)。
    """
    if not stopped or not stopped.get("start"):
        return stopped
    s = stopped["start"]
    try:
        pos = procmodel.start_pos(s.get("path") or [], s.get("block") or "",
                                  s.get("turns") or [], s.get("after"))
    except (TypeError, ValueError):
        pos = None
    # 周回数は、その位置の周まで達する数にする(1 では 5 周目の位置を拒否される)
    laps = max([1, *(pos.turns if pos is not None else ())])
    proc = stopped.get("proc") or ""
    ok = (pos is not None and project.build_safe(proc, laps, pos)[0] is not None
          and project.start_loops_match(proc, s))
    if not ok:
        return {**stopped, "start": None, "lost": True}
    view = project.build_safe(proc)[0]
    if view is not None and view.resume_points \
            and view.resume_points[0].get("uses_laps") is False:
        # 先頭から始めても周回数が反映されない手順(2 周目へ進まない。
        # startpos.laps_unused)では、どこで停止した位置からも反映されない
        stopped = {**stopped, "uses_laps": False}
    return stopped


def drawn_of(project, art, dev: str):
    """実行中のレーンの図を、1 周目として描く変換結果(SentView)。開始のときに、送る
    手順と続けて変換して、送った記録に残したものである(Coupler._build_lane)。

    送った記録に図の変換結果がなければ、今の中身で作った変換結果を、その実行の図として
    覚える(図と位置の対応付けを同じ変換結果に揃える)。変換できなければ None。ほかの周の
    図は lap_view が作る。
    """
    got = project.sent.view(art.hash, dev)
    if got is not None:
        return got
    r, _err = project.build_safe(art.name, finals=art.finals, write=False,
                                 drawn_laps=art.laps)
    if r is None:
        return None
    return project.sent.keep_view(art.hash, dev, r)


def _key(m: dict) -> tuple:
    """区切りと開始位置を、図の側の区切りと照合する組み合わせ。"""
    return (m.get("block") or "", tuple(m.get("path") or []))


# ---- レーンの図 ----
#
# 実行中のレーンの図は、決まっていることだけを描く。図の指定は、何周目として描くか
# (Figure.lap)と、その周に選んだ選択肢(Figure.chosen)の 2 つである。
# - 周は、装置が返す最後に通った区切りから出す(figure_lap)。周で変わるのは、周回数の
#   くり返しを指す「対象の回」の選ばれ方である。同じ選ばれ方の周は、送った手順の同じ
#   複製(lap_map の項目)を通るので、その項目の最初の周を代表にして、1 つの変換結果に
#   まとめる(lap_rep、lap_view)
# - 選んだ選択肢は、選択の記録(pick_of の形の並び)から取り出す。記録の出所は、
#   PC が送った選択(DeviceLink.picks_sent)、装置の状態(seen_picks)、開始位置
#   (start_picks)で、監視がレーンの記録(runstate.json)に残す(Coupler の
#   _keep_picks)。画面の状態は、残した記録と、前に描いた図の選択と、今の状態から、
#   同じ決め方で作る(picks_now)
# - 選んでいない選択待ち(選択肢が 2 つ以上)は、どの選択肢の中身も描かず、枠のすぐ
#   後ろに続きを描く(timeline.lane_picks)
#
# 図と、その上の位置(線、止まる先、周の頭と終わり)は、同じ Figure から作る。画面へは
# 図のリビジョン(Figure.rev)を渡し、画面はそれを送り返して図を受け取る(gui の
# /api/timeline)。リビジョンに指定が入っているので、サーバーは今の指定を覚えない。


@dataclass(frozen=True)
class Figure:
    """レーンの図の指定と、その変換結果。

    view は lap 周目として描く変換結果、chosen は選んだ選択肢で、(((選択待ちの
    レコード番号, 囲むくり返しの回の順番), 編集画面に並んでいる選択肢の番号), ...)
    である(timeline.lane_picks)。rows は図の行(その実行で通りうる経路が使う入力の
    名前)。
    """
    view: object
    lap: int = 1
    chosen: tuple = ()
    rows: tuple = ()

    @property
    def rev(self) -> str:
        """画面へ渡す図のリビジョン。変換結果の中身と、図の指定から作る。"""
        return f"{self.view.rev}~{fig_text(self.lap, self.chosen)}"


def fig_text(lap: int, chosen: tuple) -> str:
    """図の指定を、図のリビジョンに入れる文字列にする。"""
    return json.dumps([lap, [[index, at, arm] for (index, at), arm in chosen]],
                      separators=(",", ":"))


def fig_from_text(rev: str) -> tuple[int, tuple] | None:
    """図のリビジョン(Figure.rev)から、図の指定(何周目か, 選んだ選択肢)を読む。
    読めなければ None。"""
    try:
        lap, chosen = json.loads(rev.partition("~")[2])
    except (ValueError, TypeError, RecursionError):
        return None
    # 画面が送り返す値なので、形を確かめる。整数でない値(Infinity など)と、選択の
    # 記録の上限を超える件数は、読めない値とする
    if not (_is_int(lap) and lap >= 1 and isinstance(chosen, list)
            and len(chosen) <= PICKS_MAX
            and all(isinstance(x, list) and len(x) == 3 and all(_is_int(v) for v in x)
                    for x in chosen)):
        return None
    return lap, tuple(sorted(((i, at), arm) for i, at, arm in chosen))


def _view(drawn):
    return drawn.view if isinstance(drawn, Figure) else drawn


def drawn_timeline(drawn) -> dict:
    """図のデータ(timeline.build)。レーンの図と同じものを作る。drawn は Figure か、
    図の変換結果(何も選んでいない 1 周目の図として扱う)。

    区切りを図の上に置くとき(timeline.mark_time)に使う。図のキャッシュがあれば、
    それを使う。
    """
    fig = drawn if isinstance(drawn, Figure) else Figure(drawn)
    view = fig.view
    blob = getattr(view, "blob", None)
    if blob is None:
        # 変換の結果そのもの(テストが描画用の変換結果として渡す)は、送る形のバイト列を
        # 持たない
        blob = binfmt.encode(view.events, view.total_frames)
    return timeline.build_cached(binfmt.proc_hash(blob), blob,
                                 timeline.lane_picks(view, dict(fig.chosen)),
                                 fig.rows)


def _covers(item: dict, lap: int) -> bool:
    """周回数のくり返しの複製(対応表の項目。item)が lap 周目を実行するか。

    項目の周は first + Σ (カウンターの回 × step) で、カウンターの回は 0 から
    total - 1 までである(Project.lap_of)。束の中の区間(カウンターが 2 つ)は、
    束の回を先に決める(区間の長さは束の周期より短い)。
    """
    rest = lap - item["first"]
    for c in sorted(item.get("counters") or [], key=lambda c: -c["step"]):
        used = min(rest // c["step"], c["total"] - 1) if rest >= 0 else 0
        rest -= used * c["step"]
    return rest == 0


def lap_rep(art, lap: int) -> int:
    """lap 周目と同じ図になる周のうち、代表にする周(その周を実行する複製の最初の周)。
    複製が見つからなければ lap そのもの。"""
    for item in art.lap_map or []:
        if not item.get("exit") and _covers(item, lap):
            return item["first"]
    return lap


def laps_alike(art) -> bool:
    """どの周も同じ複製を実行するか(周で選ばれ方が変わる分岐がない)。"""
    return sum(1 for item in art.lap_map or [] if not item.get("exit")) <= 1


def first_lap(art) -> int:
    """その実行が最初に実行する周。周回数のくり返しの途中から始めた実行は、その周で
    ある。周のない実行(くり返しの後ろから始めた実行)は 1。"""
    items = [item for item in art.lap_map or [] if not item.get("exit")]
    return min(items, key=lambda x: x["start"])["first"] if items else 1


def figure_lap(art, st: dict, hold: bool = False, done: int | None = None) -> int:
    """図を何周目として描くか。図と、選択の記録の絞り込みは、どちらもこれで決める。

    - 最後に通った区切りの周(lane_lap)。周回数のくり返しを抜けた後は最後の周で、
      その周に選んだ形のまま残す
    - 周回を終えるために止まって待つ間(hold)は、周の境で止まったなら終えた周
      (ended_lap)。止まったのは次の周の先頭の区切りだが、その周はまだ始まって
      いない。選択待ちで止まったなら、上と同じく最後に通った区切り(その待ち)の周
    - 区切りをまだ通っていない間と、周回数のくり返しの手前は、最初に実行する周
    - 周回の終え方の続きで、くり返しの後ろから続けた実行は、変換に周がない。元の実行で
      終えた周(done。レーンの記録の laps_done)で描く
    """
    lap = lane_lap(art, st.get("last_mark"))
    if hold:
        lap = ended_lap(art, st) or lap
    if lap is None:
        lap = done if done is not None and not art.lap_map else first_lap(art)
    lap = max(1, lap)
    return min(lap, art.laps) if _is_int(art.laps) and art.lap_map else lap


# 周ごとの図の変換結果のキャッシュ({(装置 ID, 送った中身の id, 1 周目の図の
# リビジョン, 保存物の id, 周): SentView})。開始した時点の定義(保存物)から作り直せる
# ので、メモリーにだけ持つ。保存物の id も鍵に入れるのは、後の周にだけ通る選択肢の中の
# ラベル名だけを修正して始め直すと、送る中身も 1 周目の図も前と同じになるからである
_LAP_VIEWS: dict = {}
_LAP_VIEWS_MAX = 64
_LAP_VIEWS_LOCK = threading.Lock()


def lap_view(project, art, dev: str, lap: int):
    """その実行の図を、lap 周目として描く変換結果(SentView)。

    1 周目は、開始のときに送った記録に残した変換結果である(drawn_of)。ほかの周は、
    開始した時点の定義(保存物)から、送った手順と同じレーンの数で、その周として変換する
    (dsl.compile_procs の drawn_lap)。同じ選ばれ方の周は代表の周にまとめる(lap_rep)。
    保存物のない実行(コマンドから転送した手順)と、変換できないときは、1 周目の図を
    使う。
    """
    first = drawn_of(project, art, dev)
    rep = lap_rep(art, lap)
    if first is None or rep == 1 or not art.sources:
        return first
    key = (dev, art.hash, first.rev, art.sources, rep)
    with _LAP_VIEWS_LOCK:
        hit = _LAP_VIEWS.get(key)
    if hit is not None:
        return hit
    try:
        frozen = project.sent.frozen(art.sources)
    except ValueError:
        return first
    r, _err = frozen.build_safe(art.source_name or art.name, None, None, art.lanes,
                                finals=art.finals, write=False,
                                drawn_laps=art.laps, drawn_lap=rep)
    if r is None:
        return first
    view = SentView.of(r)
    with _LAP_VIEWS_LOCK:
        if len(_LAP_VIEWS) >= _LAP_VIEWS_MAX:
            _LAP_VIEWS.pop(next(iter(_LAP_VIEWS)))
        return _LAP_VIEWS.setdefault(key, view)


def pick_of(art, index, counters, arm: int) -> dict | None:
    """装置が、選択待ち(レコード index。そのときのカウンターの値が counters)を、
    飛び先 arm の選択肢へ抜けたことの記録。選択肢が 2 つ以上の選択待ちでなければ None
    (道が 1 本の待ちは、選ぶまでもなく決まっている)。

    記録は {"block", "path", "turns", "lap", "arm"} で、どの変換結果の上でも同じ通過を
    指定できるよう、レコード番号ではなく、ブロックの識別子と呼び出しの経路と、囲む
    くり返しのそれぞれが何回目か(turns)で持つ。図の変換結果と、周回の終え方の続きは、
    送った手順とレコード番号が違う。lap は何周目の通過か(周回数のくり返しの外は
    None)、arm は編集画面に並んでいる選択肢の番号である(飛び先の番号は、その回に
    「対象の回」が当てはまる選択肢だけを数えるので、周でずれる)。
    """
    aw = next((a for a in art.awaits or [] if a.get("index") == index), None)
    arms = list((aw or {}).get("arm_indices") or [])
    mark = next((m for m in art.marks or []
                 if m.get("index") == index and m.get("kind") == "branch"), None)
    if len(arms) < 2 or not (_is_int(arm) and 0 <= arm < len(arms)) or mark is None:
        return None
    turns = turns_of(art.loop_map, mark, counters)
    if turns is None:
        return None
    return {"block": mark.get("block") or "", "path": list(mark.get("path") or []),
            "turns": turns, "arm": arms[arm],
            "lap": Project.lap_of(art.lap_map, {"index": index,
                                                "counters": list(counters or [])})}


def _arm_ranges(events: list, awaits: list) -> list:
    """選択肢が 2 つ以上の選択待ちごとの (選択待ちのレコード番号, 選択肢ごとのレコードの
    範囲の境) の並び。境は飛び先の番号の順で、選択肢 k のレコードは 境[k] 以上
    境[k + 1] 未満である(変換は選択肢を飛び先の順に並べ、その後ろに続きを置く。
    engine.after_arms)。"""
    out = []
    for a in awaits or []:
        i = a.get("index")
        if not (_is_int(i) and 0 <= i < len(events)) \
                or len(a.get("arm_indices") or []) < 2:
            continue
        after = after_arms(events, i)
        if after is not None:
            out.append((i, [*events[i].targets, after]))
    return out


def seen_picks(project, art, st: dict, earlier=()) -> list:
    """装置の状態 st と、その前に読めた状態 earlier から分かる選択(pick_of の形の並び)。

    装置は、選択待ちを抜けた後、どの選択肢へ進んだかを返さない。ただし、最後に出した
    レコード(event_index)か最後に通った区切りが、ある選択肢のレコードの範囲にあれば、
    装置はこの回にその選択肢へ進んでいる。時間切れで装置が選んだときと、ほかの
    ツールが選んだときも、これで分かる。選択肢の中に区切りがなく、PC が読む前に
    選択肢を出たときは分からない。

    earlier は、この実行で前に読めた状態(DeviceLink.seen_states)で、送った中身が
    art のものだけを使う。監視がレーンの記録に残す前に選択肢を出ても(選択肢の中の
    待ちを、監視の周期より早く選んだときなど)、読めていた状態から分かる。
    """
    got = project.sent.records(art.hash)
    if got is None:
        return []
    ranges = None
    out = []
    for s in [*(x for x in earlier if x.get("proc") == art.hash), st]:
        lm = s.get("last_mark") or {}
        if not _is_int(lm.get("index")):
            continue
        at = [i for i in (s.get("event_index"), lm["index"]) if _is_int(i)]
        if ranges is None:
            ranges = list(_arm_ranges(got[0], art.awaits))
        for index, bounds in ranges:
            for arm in range(len(bounds) - 1):
                if any(bounds[arm] <= i < bounds[arm + 1] for i in at):
                    p = pick_of(art, index, lm.get("counters"), arm)
                    if p is not None:
                        out.append(p)
    return out


def picks_around(view, start: dict) -> list:
    """開始位置 start を中に含む選択肢の記録(pick_of の形の並び)。

    開始位置が選択待ちの選択肢の中にあれば、その選択肢を通ることは決まっている。
    view は図の変換結果で、開始位置の区切りが、どの選択待ちのどの選択肢のレコードの
    範囲にあるかで調べる。回番号を書かなかったくり返しは 1 回目とする。くり返しの直後を
    指す位置(after)は、選択肢の中ではない。
    """
    if start.get("after") or not any(len(a.get("arm_indices") or []) > 1
                                     for a in view.awaits or []):
        return []
    given = [x for x in start.get("turns") or [] if _is_int(x)]
    tl = None
    for m in view.marks or []:
        refs = list(m.get("loop_refs") or [])
        if _key(m) != _key(start) or m.get("kind") == "lap" or len(given) > len(refs):
            continue
        turns = given + [1] * (len(refs) - len(given))
        if timeline.pass_ordinal(view.loop_map, refs, turns) is None:
            continue
        if tl is None:
            tl = binfmt.decode(view.blob)[0]
        out = []
        for index, bounds in _arm_ranges(tl, view.awaits):
            arm = next((k for k in range(len(bounds) - 1)
                        if bounds[k] <= m["index"] < bounds[k + 1]), None)
            am = next((x for x in view.marks if x.get("index") == index
                       and x.get("kind") == "branch"), None)
            if arm is None or am is None:
                continue
            arefs = list(am.get("loop_refs") or [])
            aw = next(a for a in view.awaits if a.get("index") == index)
            out.append({
                "block": am.get("block") or "", "path": list(am.get("path") or []),
                "turns": turns[:len(arefs)], "arm": aw["arm_indices"][arm],
                "lap": next((turns[k] for k, ref in enumerate(arefs)
                             if k < len(turns) and view.loop_map[ref].get("is_laps")),
                            None)})
        return out
    return []


# start_picks の答えのキャッシュ({(装置 ID, 送った中身の id, 図のリビジョン):
# 記録の並び})。開始位置は実行の間変わらない。周回の終え方の続きは必ず開始位置を持ち、
# 画面の状態と監視が毎回調べるので、そのたびに図のレコードを読み直さない
_START_PICKS: dict = {}
_START_PICKS_MAX = 64


def start_picks(project, art, dev: str) -> list:
    """開始位置から始めた実行の、開始位置を中に含む選択肢の記録(picks_around)。
    先頭から始めた実行は空。"""
    at = art.start
    if at is None:
        return []
    view = lap_view(project, art, dev, first_lap(art))
    if view is None:
        return []
    key = (dev, art.hash, view.rev)
    with _LAP_VIEWS_LOCK:
        hit = _START_PICKS.get(key)
    if hit is None:
        hit = picks_around(view, {"path": list(at.path), "block": at.block,
                                  "turns": list(at.turns), "after": at.after})
        with _LAP_VIEWS_LOCK:
            if len(_START_PICKS) >= _START_PICKS_MAX:
                _START_PICKS.pop(next(iter(_START_PICKS)))
            _START_PICKS[key] = hit
    return [dict(p) for p in hit]


# 1 レーンに残す選択の記録の数の上限。今の周と、周回数のくり返しの外の分だけを残す
# ので、超えるのは、同じ周に同じ分岐をこれより多く通る手順だけである。超えた分は古い
# 方から捨て、その通過は選んでいない形で描く
PICKS_MAX = 256


def _same_pass(p: dict) -> tuple:
    return (p.get("block") or "", tuple(p.get("path") or []),
            tuple(p.get("turns") or []))


def _past_turn(art, st: dict, p: dict) -> bool:
    """選択の記録 p が、終わりのないくり返し(周回数のくり返しを除く)の、もう終わった
    回の通過か。

    終わりのないくり返しは、図に 1 回だけ描く(timeline._one_lap)。その 1 回は装置が
    いま実行している回なので、前の回に選んだ選択肢を当てると、次の回の待ちの間も、前の
    回の選択肢の中身を描いたままになる。装置がいま何回目にいるかは、最後に通った
    区切りを囲むくり返しの回番号から出す(turns_of)。記録の選択待ちと同じくり返しの
    中に装置がいて、回番号が違えば、もう終わった回である。記録の選択待ちが送った手順に
    ないとき(開始位置を中に含む選択肢の記録、続きへ持ち越した記録)と、装置がその
    くり返しの中にいるかが分からないときは、偽を返す(記録を使う)。
    """
    lm = st.get("last_mark") or {}
    # 最後に通った区切りを囲むくり返しの (識別子, 回番号)。外側から
    now: list = []
    for m in art.marks or []:
        if m.get("index") == lm.get("index"):
            turns = turns_of(art.loop_map, m, lm.get("counters")) or []
            if len(turns) > len(now):
                now = list(zip(m.get("loops") or [], turns, strict=True))
    for m in art.marks or []:
        refs = list(m.get("loop_refs") or [])
        loops = list(m.get("loops") or [])
        if m.get("kind") != "branch" or _key(m) != _key(p) \
                or len(refs) != len(p.get("turns") or []) or len(loops) != len(refs):
            continue
        for k, ref in enumerate(refs):
            if k >= len(now) or now[k][0] != loops[k]:
                break               # ここから内側は、装置のいるくり返しと別である
            item = art.loop_map[ref]
            endless = not item.get("is_laps") and any(
                c["total"] == binfmt.INFINITE_COUNT for c in item.get("counters") or [])
            if endless and now[k][1] != p["turns"][k]:
                return True
    return False


def picks_now(project, art, st: dict, lap: int, dev: str = "", kept=(),
              sent=(), seen=()) -> list:
    """今の図に使う選択の記録(pick_of の形の並び)。lap 周目の通過と、周回数のくり返しの
    外の通過だけを残す(周が替わったら、前の周の選択は捨てる)。終わりのないくり返しの
    中の通過は、今の回の分だけを残す(_past_turn)。

    kept はレーンの記録に残した分、sent は PC が送った選択(DeviceLink.picks_sent の
    (送った中身の id, 選択待ちのレコード番号, カウンターの値, 飛び先の番号) の並び)、
    seen はこの実行で前に読めた装置の状態(DeviceLink.seen_states)である。同じ通過の
    記録は、後に挙げた出所を使う: 残した分、開始位置、PC が送った選択、装置の状態。
    """
    found = [*kept, *start_picks(project, art, dev)]
    for proc, index, counters, arm in sent or ():
        p = pick_of(art, index, counters, arm) if proc == art.hash else None
        if p is not None:
            found.append(p)
    found += seen_picks(project, art, st, seen)
    out: dict = {}
    for p in found:
        if p.get("lap") in (None, lap) and not _past_turn(art, st, p):
            out.pop(_same_pass(p), None)
            out[_same_pass(p)] = p
    return list(out.values())[-PICKS_MAX:]


def chosen_on(view, picks: list, lap: int) -> tuple:
    """選択の記録 picks を、lap 周目の図の変換結果 view の上の指定(Figure.chosen)に
    する。記録の通過を、ブロックの識別子と呼び出しの経路で view の選択待ちに当て、
    囲むくり返しの回の順番を求める(timeline.pass_ordinal。周回数のくり返しは 1 周だけ
    描くので数えない)。その回を担当しない選択待ち(「対象の回」で分けた別の区間)と、
    view にない通過は、当てない。"""
    out = {}
    for p in picks or []:
        if p.get("lap") not in (None, lap):
            continue
        turns = list(p.get("turns") or [])
        for m in view.marks or []:
            refs = list(m.get("loop_refs") or [])
            if m.get("kind") != "branch" or _key(m) != _key(p) \
                    or len(refs) != len(turns):
                continue
            at = timeline.pass_ordinal(view.loop_map, refs, turns)
            if at is not None:
                out[(m["index"], at)] = p["arm"]
    return tuple(sorted(out.items()))


def arm_open(art, st: dict, fig: Figure) -> bool:
    """装置が選択肢 2 つ以上の選択待ち(最後に通った区切り)を抜けたのに、図 fig が
    その通過を、まだ選んでいない形で描いているか。時間切れで装置が選んだときと、
    ほかのツールが選んだときに、装置がその選択肢の中の区切りを通るまでの間である
    (seen_picks)。選択待ちの間は偽(線は、待っている所に置く)。"""
    lm = st.get("last_mark") or {}
    p = None if st.get("awaiting") else pick_of(art, lm.get("index"),
                                                lm.get("counters"), 0)
    if p is None:
        return False
    drawn = dict(fig.chosen)
    return not any(k in drawn for k, _arm in chosen_on(fig.view, [p], p["lap"]))


def ending_builds(project, name: str, laps, lanes: int = 1) -> list:
    """周回数が無限の実行で、周回の終え方で通りうる選択肢の中身を並べた変換結果の
    並び。行を求めるためだけの変換で、装置へは送らない。周回数が無限でないときと、
    手順が「最後の回」も「残り n 回」も使っていないときは空。

    周回数が無限の変換は、周回数のくり返しを指す「最後の回」と「残り n 回」の選択肢を
    レコードに並べない(無限のくり返しに最後の回はない)。実行中に周回の終え方で最後の
    回を作ると、その選択肢を実行する。行を初めから決めておくために、どの周も、最後の
    回の何周か前の周として解決して変換する(lapplan.LapsLeft)。上に書いた選択肢が
    当てはまる周はその選択肢へ進むので、普通の無限の変換と合わせると、通りうる
    入力の全部になる。どの周回数でも上の選択肢に隠れる選択肢の入力は入らない。

    変換は、当てはまる「最後の回」「残り n 回」の組が違う周ごとに 1 回である。最後の
    回(どれも当てはまる)と、その 1 周前(「残り 2 回」より大きいものだけ)のように、
    「残り n 回」の n の種類の数だけ増える。「最後の回」の下に書いた「残り 2 回」は、
    最後の回では上に隠れ、その 1 周前に選ばれるので、最後の回としてだけ解決すると
    入らない。変換できない組(装置の容量を超えるなど)は飛ばす。
    project は、その手順を読むプロジェクト(実行は、開始した時点の定義)である。
    変換は Project.build がキャッシュするので、同じ定義の間は 1 回だけ実行される。
    """
    if laps != procmodel.INFINITE:
        return []
    plain = project.build_safe(name, procmodel.INFINITE, None, lanes, write=False)[0]
    whens = [procmodel.When("", kind, n or 0) for kind, n in
             (plain.final_whens if plain is not None else [])]
    out, seen = [], set()
    for left in sorted({0, 1, *(w.n for w in whens if w.kind == "remaining")}):
        at = LapsLeft(left)
        hold = frozenset(w for w in whens if at.holds(w))
        if not hold or hold in seen:
            continue
        seen.add(hold)
        r = project.build_safe(name, procmodel.INFINITE, None, lanes, finals=at,
                               write=False)[0]
        if r is not None:
            out.append(r)
    return out


def _endings_of(project, art) -> list:
    """送った手順 art の実行の ending_builds。開始した時点の定義(保存物)がなければ
    空。"""
    if art.laps != procmodel.INFINITE or not art.sources:
        return []
    try:
        frozen = project.sent.frozen(art.sources)
    except ValueError:
        return []
    return ending_builds(frozen, art.source_name or art.name, art.laps, art.lanes)


# 図の行のキャッシュ({(装置 ID, 送った中身の id, 1 周目の図のリビジョン):
# 行の名前の並び})
_ROWS: dict = {}
_ROWS_MAX = 64


def figure_rows(project, art, dev: str, kept=None) -> tuple:
    """実行中のレーンの図の行(入力の名前の並び)。その実行で通りうる経路が使う入力の
    全部で、描き直しても変わらない。

    装置へ送ったレコード(どの周の、どの選択肢の中身も並んでいる)と、1 周目の図の
    変換結果(送った手順が開始位置から始まるとき、その手前も図には描く)から出す。
    周回数が無限の実行は、周回の終え方で通りうる選択肢の入力も入れる(ending_builds)。
    kept は、レーンの記録に残した行(Coupler の _record_group)で、あればそれを使う
    (周回の終え方の続きは別の手順として送るので、そのレコードだけから出すと、元の
    実行の行が減る)。
    """
    if kept:
        return tuple(kept)
    first = drawn_of(project, art, dev)
    key = (dev, art.hash, first.rev if first is not None else "")
    with _LAP_VIEWS_LOCK:
        hit = _ROWS.get(key)
    if hit is not None:
        return hit
    lists = []
    got = project.sent.records(art.hash)
    if got is not None:
        lists.append(got[0])
    for r in (first, *_endings_of(project, art)):
        if r is not None:
            lists.append(binfmt.decode(r.blob)[0])
    rows = tuple(timeline.rows_of(*lists))
    # 状態の取得と図の取得が重なるので、キャッシュの読み書きはロックの下で行う
    with _LAP_VIEWS_LOCK:
        if len(_ROWS) >= _ROWS_MAX:
            _ROWS.pop(next(iter(_ROWS)))
        _ROWS[key] = rows
    return rows


def figure_of(project, art, dev: str, lap: int, picks: list, rows=()) -> Figure | None:
    """その実行の、lap 周目の図(選択の記録 picks を当てたもの)。図の変換結果が
    なければ None。"""
    view = lap_view(project, art, dev, lap)
    if view is None:
        return None
    return Figure(view, lap_rep(art, lap), chosen_on(view, picks, lap), tuple(rows))


def figure_from_rev(project, art, dev: str, rev: str, rows=(),
                    done: int | None = None) -> Figure | None:
    """画面が送り返した図のリビジョン rev の図。done は figure_lap と同じ(続きの元の
    実行で終えた周)。

    指定を読めないときと、指定の周がこの実行の図の周(送った手順の複製の代表の周と、
    最初の図の周)でないときは、何も選んでいない最初の図にする。要求の周をそのまま
    変換に渡すと、でたらめな周の数だけ変換のキャッシュが増える。
    """
    first = lap_rep(art, figure_lap(art, {}, done=done))
    reps = {item["first"] for item in art.lap_map or [] if not item.get("exit")}
    lap, chosen = fig_from_text(rev or "") or (first, ())
    if lap != first and lap not in reps:
        lap, chosen = first, ()
    view = lap_view(project, art, dev, lap)
    return None if view is None else Figure(view, lap, chosen, tuple(rows))


def frame_of(project, art, index, dev: str = "", counters=None,
             drawn=None) -> int | None:
    """その区切りが、図の上のどのフレームに当たるかを出す。

    図は手順の全体を先頭から 1 周だけ描くのに、装置が受け取った手順は開始位置から
    始まり、周回数のぶんだけくり返す。レコード番号は両者で対応しないので、区切りを
    ブロックの識別子と呼び出しの経路で照合する。drawn は今の図(Figure)で、渡さなければ
    何も選んでいない 1 周目の図で見る。

    くり返しの中の区切りは、どの回でも同じレコードを通る。区切りを囲むくり返しの
    それぞれが何回目か(counters は装置が返したその時点のカウンターの値)から、図の上で
    その回に通る時刻を求める(timeline.mark_time)。「対象の回」の展開で同じブロックが
    複数の区切りになったときは、その回を担当する区切りを選ぶ。周の境は、図に 1 周
    だけ描くので、周の先頭なら図の最初の、周回数のくり返しを抜けた所なら最後の、
    同じ組み合わせの区切りに当たる。
    """
    if drawn is None:
        drawn = drawn_of(project, art, dev)
    if drawn is None:
        return None
    return place(drawn, art, index, counters)


def place(drawn, art, index, counters) -> int | None:
    """frame_of の中身。drawn は図(Figure か、図の変換結果)、art は装置へ送った
    成果物である。"""
    mark = Project.mark_at(art.marks, index)
    if mark is None:
        return None
    view = _view(drawn)
    same = [m for m in view.marks if _key(m) == _key(mark)]
    if not same:
        return None
    tl = drawn_timeline(drawn)
    if mark.get("kind") == "lap":
        out = Project.lap_exit(art.lap_map, {"index": index})
        m = same[-1] if out else same[0]
        return timeline.mark_time(tl, view.loop_map, m,
                                  [1] * len(m.get("loop_refs") or []))
    turns = turns_of(art.loop_map, mark, counters)
    if turns is None:
        return None
    for m in same:
        if (m.get("kind") != mark.get("kind")
                or list(m.get("loops") or []) != list(mark.get("loops") or [])):
            continue
        at = timeline.mark_time(tl, view.loop_map, m, turns)
        if at is not None:
            return at
    return None


def ended_lap(art, st: dict) -> int | None:
    """周の境で停止した実行が終えた周。

    周回を終えるために周の境で止まってほかのレーンを待つレーン(lap_end_hold)は、次の
    周の先頭の区切りに止まっている。その周はまだ 1 フレームも動いておらず、停止したのは
    前の周の終わりである。周の先頭の区切りでないとき、抜けた所のとき、1 周目の先頭
    (周回数のくり返しより前で停止した)のときは None を返す。
    """
    lm = st.get("last_mark") or {}
    if (lap_mark_at(art.marks, lm.get("index")) is None
            or Project.lap_exit(art.lap_map, lm)):
        return None
    lap = Project.lap_of(art.lap_map, lm) or 0
    return lap - 1 if lap >= 2 else None


def lap_end_frame(project, art, st: dict, dev: str = "", drawn=None) -> int | None:
    """周の境で停止した実行の、図の上の周の終わり(周回数のくり返しを抜けた所)。

    停止したのは前の周の終わりである(ended_lap)。図は周回数のくり返しを 1 周だけ描く
    ので、その終わりは、図の最後の周の境(抜けた所)に当たる。周の境で停止したのでない
    ときは None を返す。drawn は今の図(frame_of と同じ)。
    """
    if ended_lap(art, st) is None:
        return None
    mark = lap_mark_at(art.marks, (st.get("last_mark") or {}).get("index"))
    if drawn is None:
        drawn = drawn_of(project, art, dev)
    if drawn is None:
        return None
    view = _view(drawn)
    same = [m for m in view.marks
            if m.get("kind") == "lap" and _key(m) == _key(mark)]
    if len(same) < 2:
        return None
    end = same[-1]
    return timeline.mark_time(drawn_timeline(drawn), view.loop_map, end,
                              [1] * len(end.get("loop_refs") or []))


def _next_record(events: list, i: int, pick: int | None = None) -> int | None:
    """レコード i の次にたどるレコードの番号。道が決まらなければ None。

    pick は、選択肢が 2 つ以上の選択待ちで装置が進んだ選択肢(飛び先の番号)で、PC が
    選択を送ったときだけ分かる(DeviceLink.picked_arm)。
    """
    ev = events[i]
    if isinstance(ev, Await):
        if len(ev.targets) == 1:
            return ev.targets[0]
        return ev.targets[pick] if pick is not None and 0 <= pick < len(ev.targets) \
            else None
    return i + 1


def lap_span(project, art, st: dict, dev: str = "", drawn=None) -> list[int] | None:
    """図の上の周(周回数のくり返しの 1 周)の [頭, 終わり]。装置が周回数のくり返しの
    中か手前にいる間だけ返す。drawn は今の図(frame_of と同じ)。

    図は周回数のくり返しを 1 周だけ描き、装置は周回数のぶんだけくり返す。画面は、装置を
    読み直すまでの間に線が周の終わりを越えたら、最後の周でなければ周の頭へ戻して描き、
    最後の周なら後ろ(後処理)へ進める。どの周が最後かは、画面が今の周と周回数で決める。
    周回数のくり返しのない手順は、手順の全体が 1 周である。くり返しを抜けた後と、周の
    ない実行(くり返しの後ろから始めた実行と、周回を終えるための続き)は、もう周の境を
    越えないので返さない(画面は、返さなければ図の全体を 1 周として描く)。
    """
    if not art.lap_map:
        return None
    index = (st.get("last_mark") or {}).get("index")
    if isinstance(index, int) and past_laps(art.lap_map, index):
        return None
    if drawn is None:
        drawn = drawn_of(project, art, dev)
    return None if drawn is None else span_on(drawn)


def span_on(drawn) -> list[int] | None:
    """図 drawn(Figure か、図の変換結果)の上の周の [頭, 終わり]。周の境の区切りは、
    周回数のくり返しの先頭と抜けた所にある。なければ None。"""
    view = _view(drawn)
    laps = [m for m in view.marks if m.get("kind") == "lap"]
    if len(laps) < 2:
        return None
    tl = drawn_timeline(drawn)
    head, end = (timeline.mark_time(tl, view.loop_map, m,
                                    [1] * len(m.get("loop_refs") or []))
                 for m in (laps[0], laps[-1]))
    if not (isinstance(head, int) and isinstance(end, int) and head < end):
        return None
    return [head, end]


def run_start_frame(project, art, dev: str = "", drawn=None) -> int | None:
    """その実行を始めた位置が、図の上のどのフレームに当たるかを出す。

    区切りを 1 つも通っていない間の現在位置に使う。先頭から始めた実行は 0 である。
    drawn は今の図(frame_of と同じ)。
    """
    if art.start is None:
        return 0
    if drawn is None:
        drawn = drawn_of(project, art, dev)
    if drawn is None:
        return None
    at = art.start
    return start_frame(drawn, {"path": list(at.path), "block": at.block,
                               "turns": list(at.turns), "after": at.after})


def start_frame(drawn, start: dict) -> int | None:
    """開始位置が、図(drawn。Figure か、図の変換結果)の上のどのフレームに当たるかを
    出す。

    回番号を書かなかったくり返しは 1 回目とする。くり返しの直後を指す位置
    (after)は、そのくり返しの最後の区切り(抜けた所)に当たる。図が通らない所
    (選んでいない選択肢の中)の位置は None である。
    """
    view = _view(drawn)
    same = [m for m in view.marks if _key(m) == _key(start)]
    if not same:
        return None
    tl = drawn_timeline(drawn)

    def first(m):
        return timeline.mark_time(tl, view.loop_map, m,
                                  [1] * len(m.get("loop_refs") or []))
    if start.get("after"):
        return first(same[-1])
    given = list(start.get("turns") or [])
    for m in same:
        if m.get("kind") == "lap":
            return first(m)            # 周の先頭。図は 1 周だけ描く
        refs = list(m.get("loop_refs") or [])
        turns = given + [1] * (len(refs) - len(given))
        at = timeline.mark_time(tl, view.loop_map, m, turns)
        if at is not None:
            return at
    return None


def _walk_start(events: list, st: dict, pick: int | None = None,
                past_arms: bool = False) -> tuple[int, dict, int, bool] | None:
    """装置の状態 st から、装置がこの先通る道をたどる起点。

    返すのは (次にたどるレコード, カウンター, 時刻の起点, 待ちを越えるか) である。
    時刻の起点は最後に通った区切りのレコードの時刻で、区切りをまだ通っていなければ
    0(開始)である。選択待ちの間は、待ちが終わった後からたどり、待つ長さは分からない
    ので「待ちを越える」とする。選択肢が 2 つ以上の選択待ちで待っている間は、どの
    選択肢を通るかが決まっていないので None を返す。通り過ぎた直後も、どの選択肢を
    通ったかは装置の状態に出ないが、PC が選択を送っていれば分かる。pick はその選択肢
    (飛び先の番号。DeviceLink.picked_arm)で、なければ道が決まらないので None を返す。
    道が 1 本の待ち(合流点・スクリプトの待ち・選択肢が 1 つの分岐)は、その先を
    たどれる。

    past_arms が真なら、どの選択肢を通るかが決まらない選択待ちは、選択肢を飛ばして
    共有の続きからたどる(engine.after_arms。周の境だけを探すとき)。選択肢の長さが
    分からないので「待ちを越える」とする。
    """
    lm = st.get("last_mark") or {}
    counters = {i: v for i, v in enumerate(lm.get("counters") or [])}
    if st.get("awaiting"):
        i = st.get("event_index")
        if not (isinstance(i, int) and 0 <= i < len(events)
                and isinstance(events[i], Await)):
            return None
        nxt = _next_record(events, i)
        if nxt is None and past_arms:
            nxt = after_arms(events, i)
        return None if nxt is None else (nxt, counters, 0, True)
    idx = lm.get("index")
    if idx is None:
        return 0, counters, 0, False
    if not (isinstance(idx, int) and 0 <= idx < len(events)):
        return None
    # 待ちを通り過ぎた直後は、その先の 1 本の道か、PC が送った選択の選択肢
    nxt = _next_record(events, idx, pick)
    if nxt is None and past_arms:
        nxt = after_arms(events, idx)
        return None if nxt is None else (nxt, counters, events[idx].frame, True)
    return None if nxt is None else (nxt, counters, events[idx].frame, False)


def _walk_until(events: list, total: int, begin: tuple, hit,
                past_arms: bool = False) -> tuple:
    """装置が通る道を、起点 begin(_walk_start)から、hit(walk の Point) が真になる
    区切りか待ちまでたどる。返すのは (その Point, そこまでに装置が進むフレーム数) で、
    見つからなければ (None, None) である。

    フレーム数は、くり返しを進める base と、選んだ選択肢の長さを足す shift を、区切りを
    またいで持ち越して数え、起点の時刻を引く(装置と同じ解釈。engine.walk)。待ちを
    越えるとき(着いた待ちが探すものでない、起点が待っている間)は、待つ長さが分からない
    ので None にする(0 と数えない)。区切りを _STOP_TARGET_MARKS 個まで通り過ぎても
    見つからなければ、あきらめる(無限のくり返しで見つからない手順をたどり続けない)。
    past_arms は _walk_start と同じで、どの選択肢を通るかが決まらない選択待ちの先を、
    共有の続きからたどる。
    """
    idx, counters, start, waited = begin
    base = shift = 0
    for _ in range(_STOP_TARGET_MARKS):
        try:
            point = walk(events, idx, base, shift, counters, total)
        except (WalkError, ValueError, IndexError):
            return None, None
        if point.kind not in ("mark", "await"):
            return None, None           # 手順の終わりまで見つからなかった
        if hit(point):
            return point, None if waited else point.frame - start
        if point.kind == "await":
            waited = True
        idx, base, shift = _next_record(events, point.index), point.base, point.shift
        if idx is None and past_arms:
            idx = after_arms(events, point.index)
        if idx is None:
            return None, None           # 先の道が決まらない
    return None, None


def stop_target(project, art, st: dict, dev: str = "", pick: int | None = None,
                drawn=None) -> dict | None:
    """「区切りで停止」を予約したとき、次に停止する位置。

    予約のマスクと、装置が返す「最後に通った区切り」と、装置へ送ったレコードの
    3 つから求める。マスクが違えば止まる先も違う。画面の「区切りで停止」は
    3 種すべてのマスクなので次の区切りになり、完了による連動停止は周の境だけの
    マスクなので今の周の終わりになる。

    マスクに当てはまらない区切りは通り過ぎる。周の境だけのマスク(完了による連動
    停止)では、途中のラベルでは停止せず、今の周の終わりが止まる先になる。

    選択肢が 2 つ以上の選択待ちを通り過ぎた直後は、どの選択肢を選んだかが装置の
    状態に出ない。PC が選択を送っていれば、その選択肢(pick。飛び先の番号。
    DeviceLink.picked_arm)をたどる。送っていなければ(時間切れで装置が選んだなど)、
    たどる道が決まらないので求めない(None を返す)。次の区切りを通ればまた出る。
    道が 1 本の待ち(合流点・スクリプトの待ち・選択肢が 1 つの分岐)は、その先を
    たどれる。選択待ち自体は区切りなので、3 種すべてのマスクではその選択待ちが
    止まる先になる。周の境だけのマスクは選択待ちでは停止せず、どの選択肢を通っても
    停止するのはその先の周の境なので、道が決まらない選択待ちは選択肢を飛ばしてたどる
    (待ちの手前でも、待つ間でも、選択を知らずに通り過ぎた直後でも止まる先を出す)。

    止まる先は図の上に縦線で出すので、図の上の位置(frame)も付ける。dev は
    装置 ID、drawn は今の図である(frame_of)。図が通らない所(まだ選んでいない
    選択肢の中)の止まる先は、図の上の位置を持たない。装置がそこに着くまでに
    進むフレーム数(frames。最後に通った区切りから。区切りをまだ通っていなければ
    開始から)も付ける。画面の終了予定は、これを区切りを通った時刻に足して出す
    (guistate)。同じたどりで数えるので、その周に選ばれる選択肢の長さもくり返しの回も
    装置と同じに入り、図の上の位置の比べが要らない(図の選択肢が空の分岐のすぐ前と
    すぐ後ろは、図の上で同じ所に並ぶ)。待ちを越える先は、待つ長さが分からないので
    None である(_walk_until)。
    """
    mask = int(st.get("stop_at_mark") or 0)
    if not mask or not st.get("running"):
        return None
    lm = st.get("last_mark") or {}

    def found(index, counters: list, frames) -> dict | None:
        got = mark_info(art, index, counters)
        if got is not None:
            got["frame"] = frame_of(project, art, index, dev, counters, drawn)
            got["frames"] = frames
        return got

    if st.get("awaiting") and mask & binfmt.MARK_AWAIT:
        # 待ちの間に予約したときは、マスクが選択待ちを含めばその場で停止する。待って
        # いる間の時間は数えない
        return found(st.get("event_index"), lm.get("counters"), None)
    got = project.sent.records(art.hash)
    if got is None:
        return None
    events, total = got
    # 周の境だけのマスク(完了による連動停止)は、待ちの間は待ちでは停止せず、待ちが
    # 終わった後の周の終わりで停止する。選択肢が 2 つ以上でも、停止する周の境は選択肢に
    # よらないので、選択肢を飛ばしてたどる(engine.after_arms)。着くまでのフレーム数は、
    # 待つ長さと選ぶ選択肢で変わるので数えない(画面は終了予定を出さない)
    lap_only = mask == binfmt.MARK_LAP
    begin = _walk_start(events, st, pick, past_arms=lap_only)
    if begin is None:
        return None
    counters = begin[1]
    point, frames = _walk_until(events, total, begin, lambda p: p.mark & mask,
                                past_arms=lap_only)
    if point is None:
        return None
    return found(point.index,
                 [counters.get(i, 0) for i in range(max(counters) + 1)]
                 if counters else [], frames)


def lap_end_stop(project, art, st: dict, pick: int | None = None
                 ) -> tuple[dict | None, int | None]:
    """周回を終える予約(lapend.MASK。次の選択待ちか周の境)で装置が停止する区切りと、
    そこに着くまでに進むフレーム数。区切りは STATUS の last_mark の形
    ({"index", "counters"})で、停止した後に装置が返すものと同じである。

    stop_target と同じたどりで数える(最後に通った区切りから。待ちを越えるときと、道が
    決まらないときは求めない。選択肢が 2 つ以上の選択待ちを通り過ぎた直後は、PC が
    送った選択 pick をたどる)。予約がまだ装置に送られていなくても、送られればそこで
    停止する。
    求められなければ (None, None) を返す。
    """
    got = project.sent.records(art.hash)
    if got is None:
        return None, None
    events, total = got
    begin = _walk_start(events, st, pick)
    if begin is None:
        return None, None
    counters = begin[1]
    mask = binfmt.MARK_AWAIT | binfmt.MARK_LAP
    point, frames = _walk_until(events, total, begin, lambda p: p.mark & mask)
    if point is None or frames is None:
        return None, None
    return {"index": point.index,
            "counters": [counters.get(i, 0) for i in range(max(counters) + 1)]
            if counters else []}, frames


# lap_end_frames の答えのキャッシュ({鍵: (送ったレコード, 答え)})。画面の状態は毎秒
# (開いているタブごとに)作られ、停止する周まで 1 周に 0.1 ミリ秒ほどかかる(1200
# レコードの周で 50 周 5 ミリ秒、区切りの上限であきらめるまで 50 ミリ秒ほど)。答えは
# 最後に通った区切りが変わるまで変わらない
_LAP_END_MEMO: dict = {}
_LAP_END_MEMO_MAX = 64


def lap_end_frames(project, art, st: dict, lap: int, pick: int | None = None
                   ) -> int | None:
    """完了に連動して、停止する周に入るのを待つレーン(連動停止の later)が、lap 周目の
    終わり(次の周の境)に着くまでに進むフレーム数。最後に通った区切りから数える
    (区切りをまだ通っていなければ開始から)。

    そのレーンは、lap 周目に入ると周の境だけの予約を受け、その周の終わりで停止する
    (Coupler._send_later)。装置にはまだ予約がないので止まる先は出さないが、停止する
    時刻は分かる。停止する所は、今の周(周回数のくり返しの手前は 0 周目)から数えて
    lap - 今の周 + 1 個目の周の境で、stop_target と同じたどりで数える(その周に選ばれる
    選択肢の長さも装置と同じに入る)。待ちを越えるとき、道が決まらないとき、その周を
    もう過ぎたときは None を返す。pick は stop_target と同じ(最後に通った選択待ちで
    PC が送った選択)。
    """
    got = project.sent.records(art.hash)
    if got is None:
        return None
    events, total = got
    lm = st.get("last_mark") or {}
    key = (id(events), lm.get("index"), tuple(lm.get("counters") or ()),
           bool(st.get("awaiting")), st.get("event_index"), lap, pick)
    hit_memo = _LAP_END_MEMO.get(key)
    if hit_memo is not None and hit_memo[0] is events:
        return hit_memo[1]
    left = lap - (Project.lap_of(art.lap_map, lm) or 0) + 1
    begin = _walk_start(events, st, pick)
    if left < 1 or begin is None:
        return None

    def hit(point) -> bool:
        nonlocal left
        if point.mark & binfmt.MARK_LAP:
            left -= 1
        return left == 0
    out = _walk_until(events, total, begin, hit)[1]
    if len(_LAP_END_MEMO) >= _LAP_END_MEMO_MAX:
        _LAP_END_MEMO.clear()
    # レコードの並びもキャッシュする(id は、並びが捨てられた後に別の並びで
    # 使い回されうる)
    _LAP_END_MEMO[key] = (events, out)
    return out


def lane_lap(art, last_mark: dict | None) -> int | None:
    """レーンの見出しに出す周(何周目か)。最後に通った区切りの周(Project.lap_of)で、
    周回数のくり返しを抜けた後(後処理の中のラベルを通った後)は、抜けた所と同じ周
    (最後の周)である。

    対応表が周を持つのは、くり返しの中と抜けた所のレコードだけなので、後処理の中の
    ラベルでは周が分からない。周がないと、見出しは「0 / 3 周」になり、画面は最後の
    周かを決められずに線を周の頭へ戻す。抜けた所の周は、その時点のカウンターでは調べない
    (後処理の中のくり返しが、周回数のカウンターを使い回すことがある)。くり返しの
    手前と、区切りをまだ通っていないときは None である。
    """
    lap = Project.lap_of(art.lap_map, last_mark)
    index = (last_mark or {}).get("index")
    if lap is not None or not isinstance(index, int) \
            or not past_laps(art.lap_map, index):
        return lap
    out = next((x for x in art.lap_map or [] if x.get("exit")), None)
    return None if out is None else Project.lap_of(art.lap_map, {"index": out["start"]})
