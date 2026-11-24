"""実行中に周回数を変える(周回を終える。coupling.md D11)。

選ぶと、グループの記録に lap_end を置き、全レーンに「次の選択待ちか周の境で停止する」
予約(lapend.MASK)を送る。全レーンがその予約で停止したら、停止した位置から新しい周回数で
続きを始める(_restart)。利用者には 1 回の実行に見せるので、停止した所の終わりは記録
しない。続きは開始した時点の定義から変換する。

lap_end は {"mode", "plans": {装置 ID: [周回数(無限は None), 解決するか]},
"script": [スクリプトの待ちの途中で、予約を後回しにしたレーン],
"waited": {装置 ID: [予約の時点で待っていた秒数, その待ちの区切りのレコード番号,
カウンターの値]}, "since": {装置 ID: 元の実行の開始時刻}, "notes": {装置 ID:
新しい周回数で開始したときの開始のメッセージ}} である。runstate.json に置くので、
サーバーを起動し直しても続きを始められる。開始のメッセージは、選んだ時点で画面に出す
ために置く(Coupler.snapshot。続きの開始も同じものを求める)。

グループでは全レーンが停止してから続きを始めるので、先に停止したレーンは待つ。その間も
1 回の実行の途中なので、画面へは実行中として渡す(holds_and_errors)。
"""
from __future__ import annotations

import json
import time

from . import lapend, positions, proto
from .client import DeviceError, failure_text
from .condwatch import current_await
from .procmodel import INFINITE
from .project import Project
from .words import sentences


def _stored_plan(plan: tuple) -> list:
    """予定 (周回数, 解決するか) を、lap_end に置く形にする(無限は None)。"""
    laps, finals = plan
    return [None if laps == INFINITE else laps, finals]


def _plan_of_stored(stored: list) -> tuple:
    """lap_end に置いた形を、予定 (周回数, 解決するか) に戻す。"""
    return (INFINITE if stored[0] is None else stored[0], stored[1])


# 続きを始めている途中で、周回の終え方を選べないときの文
_RESTARTING = "周回の終え方を反映している途中のため、選択できません"
# 周回の終え方を選べない理由の文。メニュー(menus。画面の ▾ はこの文を出し、自分では
# 持たない)と、選んだときの拒否の文(_change)の両方で使う
_OWNED = "操作権がないため、周回の終え方を変更できません"
_LOST = "未検出のレーンがあるため、周回の終え方を変更できません"
_NO_REPLY = "応答のないレーンがあるため、周回の終え方を変更できません"
_UNREADABLE = "装置へ送った手順のコピーを読み込めないため、周回の終え方を変更できません"
_RESERVED = "区切りで停止を予約しているため、周回を終えられません"
_IDLE = "動いていないレーンがあるため、周回を終えられません"
_HOLDING = "全レーンの停止を待っているため、選択できません"
_AFTER = "周回数のくり返しを抜けたため、周回を終えられません"
_AFTER_REVERT = "周回数のくり返しを抜けたため、周回数を戻せません"
_LAST = "最後の周のため、周回を早く終えられません"
# 今の周を決められないグループのメニュー(menus)
_NO_VIEW = {"lap": 0, "kind": "", "span": 0, "after": False, "options": [],
            "holding": False}


def _run_of(g: dict) -> tuple:
    """グループの実行を表す値(レーンが動かす手順の id の並びと、グループを始めた
    時刻)。送った手順のコピーが読めない理由を、同じ実行について 1 回だけ出すのに使う
    (positions.tell_bad_copy)。同じ手順を始め直した実行は別の実行である(画面の状態の
    側も、手順の id と始めた時刻で区別する。guistate の _tell_bad_copy)。"""
    return (tuple(x.get("proc_id") for x in g.get("lanes") or []),
            g.get("started_at"))


def _revert_laps(backs: list):
    """戻す項目に出す周回数。レーンによって戻す先が違えば、数は示さない。"""
    return backs[0] if len({repr(b) for b in backs}) == 1 else "mixed"


def _taking_part(g: dict) -> list:
    """グループ g のうち、周回の終え方に加わっているレーンの記録(lap_end の plans に
    あるレーン)。終え方を選んでいなければ、人がレーンを指して止め方を押して停止した
    レーン(レーンの記録の hand_stop があり、終わりを書いた)を除いたレーンである。
    終え方を選ぶとき、そのレーンはもう来ない相手として計画の対象にしない(残って
    動いているレーンだけで終える)。区切りで停止を押してまだ動いているレーンは除かない
    (人の予約があるので、終え方を選べない。LapEnds._blocked)。

    待つ間にレーンの止め方で停止したレーンも、計画から外れる(LapEnds.stop_of)。外れた
    レーンは待たず、続きにも加わらない。
    """
    le = g.get("lap_end")
    if not le:
        return [x for x in g.get("lanes") or []
                if not (x.get("hand_stop") and x.get("ended"))]
    plans = le.get("plans") or {}
    return [x for x in g.get("lanes") or [] if x.get("dev") in plans]


class LapEnds:
    """グループの周回の終え方の変更。

    Coupler の一部を分けたもので、Coupler のロックと、動いているグループの記録を
    そのまま使う。この中の欄(_restarting など)は Coupler も読む。
    """

    # 続きを始めている途中に、戻す項目か、一部のレーンの止め方が押されたとき、始め
    # 終えるのを待つ長さ(秒。change、wait_restart)。始めるのは転送と開始の要求だけ
    # なので、普通は 1 秒もかからない
    RESTART_WAIT_S = 15.0

    def __init__(self, coupler):
        self.c = coupler
        self._resumed_waits: set[str] = set()
        self._restarting: set[str] = set()
        self._restart_cancel: set[str] = set()
        # 続きの RUN を送り始めたグループ(id)。この後に押された止め方は、続きが
        # 始まった後の止め方として扱う(begin_runs)
        self._runs_begun: set[str] = set()
        # そのグループで止め方が押されたときに、書かずに保持した「周回数を元に戻した行」
        # ({グループの id: (行の時刻, [(装置 ID, 周回数)])}。drop_of、_end_cancelled)
        self._backs_kept: dict[str, tuple] = {}
        # 続きの変換のキャッシュ({鍵: (種類, 変換の結果)}。_rest_build)
        self._rest_memo: dict = {}

    def _view(self, g: dict, by_id: dict,
                      fresh: dict | None = None) -> dict | None:
        """グループの周回数を変える計算に使う値。

        全レーンが動いていなければ None を返す(停止している実行の周回数は変え
        られない)。fresh は装置 ID -> いま読んだ状態で、ないレーンは収集した状態を
        使う。今の周(lap)は一番進んでいるレーンの周で、周回数のくり返しにまだ
        入っていないレーンは 1 周目とみなす。

        レーンの running は装置で動いている手順の (周回数, 解決するか)、plan は選んだ
        終え方を含めた今の予定である。選んで、まだ続きから始め直していない間は
        両者が違う。メニューは予定から作り、今の周の当てはまり方が変わるかは、
        動いている手順と比べる(切り替えるまでは、その手順が周を進める)。
        """
        pending = (g.get("lap_end") or {}).get("plans") or {}
        lanes = []
        for x in _taking_part(g):
            link = by_id.get(x.get("dev"))
            st = (fresh or {}).get(x["dev"]) or (link.status if link else None) or {}
            if (not g.get("lap_end") and x.get("hand_stop") and link is not None
                    and not link.error
                    and not (st.get("running") or st.get("awaiting"))):
                # 人が押した止め方で停止し、まだ終わりを書いていないレーン(書くのは
                # 停止して 1.5 秒後)。書いた後と同じく、もう来ない相手として除く
                continue
            if link is None or link.error:
                return None
            if not (st.get("running") or st.get("awaiting")):
                return None
            art = self.c.project.sent.built(x.get("proc_id") or "", link.registered_id)
            if art is None:
                return None
            lm = st.get("last_mark") or {}
            lanes.append({"dev": x["dev"], "link": link, "lane": x, "art": art,
                          "st": st, "lap": Project.lap_of(art.lap_map, lm),
                          "after": positions.past_laps(art.lap_map, lm.get("index")),
                          "running": (art.laps, art.finals),
                          "plan": (_plan_of_stored(pending[x["dev"]])
                                   if x["dev"] in pending
                                   else (art.laps, art.finals))})
        if not lanes:
            return None
        whens = [w for ln in lanes for w in ln["art"].final_whens]
        return {"lanes": lanes, "lap": max(ln["lap"] or 1 for ln in lanes),
                "span": lapend.span_of(whens), "kind": lapend.kind_of(whens),
                "after": any(ln["after"] for ln in lanes)}

    def _options(self, view: dict) -> list[dict]:
        """選べる終え方。{"mode", "laps"(無限は None)} に、選べない理由 why が付く。

        今と同じになる終え方と、周回数を延ばすことになる終え方は出さない。周回数を
        変えず、今の周にまだ通っていない「最後の回」「残り n 回」の分岐もない終え方
        (最後の周で分岐を通った後の「最後の回を飛ばして」)も、何も変えないので
        出さない。レーンの周が揃っていないと、「今から」「飛ばして」は今の周を指す
        ので、レーンごとに指す周が変わる。押せなくする(全レーンを同じ周回数で終える)。
        「N 周目を最後の回にして終える」は周を数で示し、全レーンの周回数を N にする
        だけなので、遅れたレーンでも N 周目が最後の回になり、意味はレーンごとに
        変わらない(最後の回を使わないグループの「N 周目で終える」と同じ)。
        戻す項目は、周回数が変わったときだけ出す。
        """
        k, span = view["lap"], view["span"]
        uneven = len({ln["lap"] or 1 for ln in view["lanes"]}) > 1
        out = []
        for mode in (("next", "now", "skip") if span else ("end",)):
            new = lapend.plan_of(mode, k, span, None)
            if all(ln["plan"] == new for ln in view["lanes"]):
                continue
            if any(ln["plan"][0] != INFINITE and new[0] > ln["plan"][0]
                   for ln in view["lanes"]):
                continue
            if not view.get("ahead") and not any(
                    self._takes_effect(ln, new) for ln in view["lanes"]):
                continue
            item = {"mode": mode, "laps": new[0]}
            why = ("レーンの周が一致していないため、選択できません"
                   if uneven and mode in ("now", "skip")
                   else self._switch_blocked(view, new))
            if why:
                item["why"] = why
            out.append(item)
        backs = [ln["lane"]["orig_laps"] for ln in view["lanes"]
                 if "orig_laps" in ln["lane"]]
        if backs and any(
                ln["plan"][0] != lapend.plan_of("revert", k, span, ln["lane"].get(
                    "orig_laps", ln["lane"].get("laps")))[0]
                for ln in view["lanes"]):
            out.append({"mode": "revert", "laps": _revert_laps(backs)})
        return out

    def _takes_effect(self, ln: dict, new: tuple) -> bool:
        """そのレーンで、新しい予定 new が今の予定から何かを変えるか。

        周回数が変われば変える。周回数が同じなら、今の周の「最後の回」「残り n 回」の
        当てはまり方が変わり、その分岐をまだ通っていないときだけ変える。周回数の
        くり返しにまだ入っていないレーンは、1 周目の分岐をどれも通っていない。
        送った手順のコピーが読めなければ、変えるものとする(選べなくはしない)。
        """
        if ln["plan"][0] != new[0]:
            return True
        art, st = ln["art"], ln["st"]
        if not lapend.changes_lap(art.final_whens, ln["lap"] or 1, ln["plan"], new):
            return False
        recs = self.c.project.sent.records(art.hash)
        index = st.get("event_index")
        if ln["lap"] is None or recs is None or not isinstance(index, int):
            return True
        awaits = [a.get("index") for a in art.awaits or [] if a.get("final")]
        return lapend.final_ahead_in_lap(
            recs[0], art.final_branches, awaits, index,
            lapend.frames_since(recs[0], st), bool(st.get("awaiting")))

    def _switch_blocked(self, view: dict, new: tuple) -> str:
        """その終え方(新しい予定 new)を選べない理由。選べれば空文字。

        今の周の「最後の回」の当てはまり方が変わるレーンについて、続きへ切り替える
        前にその分岐を通るかを調べる(もう通った分岐は数えない。押す前に通った分岐は
        普通の周のままである)。通るなら、今の周はもう元の当てはまり方で
        実行されるので、項目の名前どおりにならない(「今から最後の回にして終える」と、
        最後の周に入った続きの「最後の回を飛ばして終える」)。選択待ちの途中の
        レーンはそこで停止するので通らない。スクリプトのブロックの待ちは、スクリプトが
        終わってから停止するので、その続きから調べる。戻す項目には当てない。戻すのを
        やめると、取り消す手段がなくなる。
        """
        for ln in view["lanes"]:
            if not lapend.changes_lap(ln["art"].final_whens, ln["lap"] or 1,
                                      ln["running"], new):
                continue
            st, art = ln["st"], ln["art"]
            index = st.get("event_index")
            if st.get("awaiting"):
                got = current_await(self.c.project, st, ln["dev"])
                if not (got and got[1].get("script")):
                    continue
            # 装置へ送ったレコードをたどる。読めなければ、通らないとは言えない
            recs = self.c.project.sent.records(art.hash)
            events = recs[0] if recs is not None else None
            if events is not None and st.get("awaiting"):
                # スクリプトが終わった後に進む先は、まだ実行していない
                index = lapend.continuation(events, index)
                kind = (lapend.final_before_stop(events, art.final_branches, index,
                                                 sent=False)
                        if isinstance(index, int) else view["kind"])
            elif events is not None and isinstance(index, int):
                kind = lapend.final_before_stop(events, art.final_branches, index,
                                                lapend.frames_since(events, st))
            else:
                kind = view["kind"]
            if kind:
                what = "最後の回" if kind == "last" else "残り n 回"
                return (f"次の選択待ちか周の境より前に「{what}」の分岐を通るため、"
                        "選択できません")
        return ""

    def menus(self, groups: list | None = None, by_id: dict | None = None,
              restarting: set | None = None) -> dict:
        """グループごとの周回の終え方。{グループの識別子: 中身} を画面へ渡す。

        中身の欄は次のとおりである。
        - lap: 今の周
        - kind: 対象の回の種類("last"、"remaining"、"both" か空)
        - span: 「残り n 回」の n(lapend.span_of)
        - options: 今の周の項目の並び
        - ahead: {"lap": 次の周, "options": 次の周に入ったときの終え方(_ahead)}
        - after: 周回数のくり返しを抜けたレーンがあるか
        - pending: 選んで、まだ始め直していない終え方
        - pending_laps: {レーンの名前: その周回数(無限は None)}
        - holding: 予約があるのに今の周を決められないか。先に停止したレーンがほかの
          レーンを待つ間で、未検出のレーンがいるときと、送った手順のコピーが読めない
          ときも含む
        - why: どの周でも項目を出せない理由。操作権、未検出、区切りで停止の予約、動いて
          いないレーンと終わりを記録したグループ、くり返しを抜けた、の順に見る。待つ間の
          グループでは、元に戻す項目のほかを選べない理由である(画面は、待つ間の
          グループと待っていないグループをまとめて選ばせない)
        - none_why: 今の周か次の周(ahead)の項目が 1 つもないときの理由。画面が見出しの
          周で選んだ項目が空のときに出す
        - hold_why: 終え方を選んだグループの、待ちの上で止まって待つレーンの選択肢を
          選べない理由(_hold_why)

        待っている間に選べるのは、元の周回数に戻す項目だけである(_held_revert)。
        送った手順のコピーが読めない形のレーンがあるグループは、読めないとき(_view)と
        同じく今の周を決めない。どのグループも含める(画面は動いているグループだけを
        見る)。groups、by_id({装置 ID: 装置の写し})、restarting(続きを始めている
        装置)は Coupler.view で同じ時点に写したもので、渡さなければここで読む。

        理由の文は、その終え方を選んだときの拒否の文(_change)と同じ文で、画面の ▾ は
        これを出す(画面は文を持たない)。
        """
        if by_id is None:
            by_id = self.c._by_id()
        names = self.c._names()
        if groups is None:
            groups = self.c.groups_copy()
        out = {}
        for g in groups:
            le = g.get("lap_end") or {}
            if restarting is None:
                with self.c._lock:
                    now = set(self._restarting)
            else:
                now = restarting
            starting = any(x.get("dev") in now for x in g.get("lanes") or [])
            pending = {
                "pending": le.get("mode") or "",
                "pending_laps": {names.get(dev, dev): plan[0] for dev, plan
                                 in (le.get("plans") or {}).items()},
                "hold_why": self._hold_why(g, by_id, starting) if le else ""}
            if starting:
                # 続きを始めている間(転送と開始の途中)は、元の周回数に戻す項目も
                # 出さない。選んでも、始め終えるのを待ってから拒否することになる
                # (_change)
                out[g.get("id", "")] = {**_NO_VIEW, "why": _RESTARTING, **pending}
                continue
            why = self._blocked(g, le, by_id)
            if why in (_LOST, _NO_REPLY) and le and self._held_revert(g):
                # 応答のないレーンがあっても、元の周回数に戻すことはできる(装置を
                # 読まずに決められる。待つ間に戻すときと同じく、全レーンが周の境で
                # 揃ってから元の周回数で続ける。_revert_held)
                out[g.get("id", "")] = {**_NO_VIEW, "options": self._held_revert(g),
                                        "holding": True, "why": why, **pending}
                continue
            if why:
                out[g.get("id", "")] = {**_NO_VIEW, "why": why, **pending}
                continue
            unread = False
            try:
                view = self._view(g, by_id)
                menu = None if view is None else {
                    "lap": view["lap"], "kind": view["kind"], "span": view["span"],
                    "after": view["after"],
                    "options": [] if view["after"] else self._options(view),
                    "ahead": {"lap": view["lap"] + 1,
                              "options": [] if view["after"] else self._ahead(view)},
                    "holding": False,
                    "why": _AFTER if view["after"] else "", "none_why": _LAST}
            except positions.BAD_COPY:
                positions.tell_bad_copy(("menus", g.get("id", "")), _run_of(g))
                menu, unread = None, True
            why = self._why_no_view(g, by_id) if menu is None else ""
            if unread or why == _UNREADABLE:
                out[g.get("id", "")] = {**_NO_VIEW, "why": _UNREADABLE, **pending}
            elif menu is None and not le:
                out[g.get("id", "")] = {**_NO_VIEW, "why": _IDLE, **pending}
            elif menu is None:
                # 全レーンが止まっていれば、続きを始めるところである(レーン 1 本の
                # グループも)。待つ相手がいないので「全レーンの停止を待っている」とは
                # 示さない
                waiting = self._waiting_for_others(g, by_id)
                out[g.get("id", "")] = {**_NO_VIEW, "options": self._held_revert(g),
                                        "holding": True,
                                        "why": _HOLDING if waiting else _RESTARTING,
                                        **pending}
            else:
                out[g.get("id", "")] = {**menu, **pending}
        return out

    def _blocked(self, g: dict, le: dict, by_id: dict) -> str:
        """グループ g の周回の終え方を、今の周によらず選べない理由(menus の why)。
        なければ空文字。

        続きの開始には操作権が要る。未検出のレーンは動いているかが分からない。人が
        予約した止め方は黙って置き換えない(周回を終える予約(lapend.MASK)は、終え方を
        選んだ間は「区切りで停止」の予約に数えない。画面も見せない)。終わりを記録した
        グループは、レーンがまだ動いていても選ばせない(change)。終え方から外れたレーン
        (_taking_part)は見ない。
        """
        links = [by_id[x["dev"]] for x in _taking_part(g) if x.get("dev") in by_id]
        if any(link.owned_by_other for link in links):
            return _OWNED
        if any(link.gone for link in links):
            return _LOST
        if any(link.error for link in links):
            # 応答がないだけのレーン(まだ未検出ではない)。今の周も停止したかも読めない
            return _NO_REPLY
        if self.c._closed(g):
            # 連動停止を始めたグループ。まだ動いているレーンにある予約は、人が押した
            # 「区切りで停止」ではなく連動停止の予約である
            return _IDLE
        if any((link.status or {}).get("stop_at_mark")
               and not (le and (link.status or {}).get("stop_at_mark") == lapend.MASK)
               for link in links):
            return _RESERVED
        return ""

    def _hold_why(self, g: dict, by_id: dict, restarting: bool) -> str:
        """周回を終えるために待ちの上で止まって待つレーンの、選択肢を選べない理由
        (menus の hold_why。画面が選択肢のボタンに出す)。

        why は終え方を選べない理由で、未検出や操作権のことも示す。待っているレーンの
        選択肢が選べないのは、どの場合も、続きがその待ちから始まるのを待っているから
        である。まだ動いているレーンか、戻るのを待つ未検出のレーンがあれば全レーンの
        停止を待っていて、なければ続きを始めるところである。
        """
        if restarting:
            return _RESTARTING
        links = [by_id[x["dev"]] for x in _taking_part(g) if x.get("dev") in by_id]
        if self._waiting_for_others(g, by_id) or any(link.error for link in links):
            return _HOLDING
        return _RESTARTING

    def _waiting_for_others(self, g: dict, by_id: dict) -> bool:
        """周回を終えるのを待つグループに、まだ止まっていないレーン(動いている)が
        あるか。終え方から外れたレーンは、動いていても待たない。"""
        for x in _taking_part(g):
            link = by_id.get(x.get("dev"))
            st = (link.status or {}) if link is not None else {}
            if st.get("running") or st.get("awaiting"):
                return True
        return False

    def _why_no_view(self, g: dict, by_id: dict) -> str:
        """_view が結果を作れなかった理由(menus と _change の文)。送った手順のコピーが
        読めないレーンは、動いていないとは示さない(_UNREADABLE)。それ以外は空(動いて
        いないレーンがある)。応答のないレーンは、先に _blocked が示す。"""
        for x in _taking_part(g):
            link = by_id.get(x.get("dev"))
            if link is None:
                continue
            if self.c.project.sent.built(x.get("proc_id") or "",
                                         link.registered_id) is None:
                return _UNREADABLE
        return ""

    def _ahead(self, view: dict) -> list[dict]:
        """次の周に入ったときに選べる終え方(_options と同じ形)。

        画面の見出しの周は、経過で補間して、装置を読み直す前に次の周へ進む。その間も
        メニューの項目を見出しの周で出せるよう、一番進んでいるレーンが次の周に入った
        ものとして出す。選べない理由(_switch_blocked)は、最後に読んだ装置の位置から
        調べる。押したときは、いま読んだ状態で改めて決める(change)。
        """
        k = view["lap"]
        lanes = [dict(ln, lap=k + 1) if (ln["lap"] or 1) == k else ln
                 for ln in view["lanes"]]
        # 次の周の分岐は、どれもまだ通っていない(_takes_effect で調べない)
        return self._options({**view, "lap": k + 1, "lanes": lanes, "ahead": True})

    def holds_and_errors(self, groups: list | None = None,
                         by_id: dict | None = None) -> tuple[dict, dict]:
        """周回を終えるために予約で停止し、続きを待っているレーンと、続きを
        始められなかった理由を、同じ時点で返す。

        待っているレーンは {装置 ID: 元の実行の開始時刻}。続きを始めるまでも
        1 回の実行の途中なので、画面へは実行中として渡す(停止した位置を出さず、
        開始を押させない)。待つ間に応答がなくなったレーンも、最後に読めた状態で待って
        いたなら含める(画面は、動いていたレーンと同じく、最後に読めた進み具合と未検出の
        文を出す。含めないと、待機中と同じ見た目になり、周回数の欄を書き替えられる)。
        人の止め方などで待つ間が終わった後も、未検出で終わりをまだ記録していない
        レーンは含める。
        理由は {表示名: 理由}。
        2 つを別々に読むと、その間に続きが拒否されたとき、「待っている(実行中)」と
        「続きを始められず停止した」が 1 つの状態に並ぶ。_failed は予約を
        消すのと理由を書くのを 1 つの排他の中で行うので、ここも同じ時点の
        グループの記録から両方を読む。groups と by_id({装置 ID: 装置の写し})は
        Coupler.view で同じ時点に写したもので、渡さなければここで読む。
        """
        if by_id is None:
            by_id = self.c._by_id()
        names = self.c._names()
        if groups is None:
            groups = self.c.groups_copy()
        errors = {names[x["dev"]]: x["lap_end_error"] for g in groups
                  for x in g.get("lanes") or []
                  if x.get("lap_end_error") and names.get(x.get("dev"))}
        out = {}
        for g in groups:
            le = g.get("lap_end")
            for x in g.get("lanes") or []:
                link = by_id.get(x.get("dev"))
                st = (link.status or {}) if link is not None else {}
                if link is None or not lapend.held(st) or x.get("ended"):
                    # 終わりを記録したレーン(続きを始めるときに、後ろに何もなく完了と
                    # 書いたレーンも)は、もう待っていない
                    continue
                # 待つ間にレーンの止め方で停止したレーンは、終え方から外れて、もう待って
                # いない(続きに加わらない。手動操作も開始もできる)
                taking = bool(le) and x.get("dev") in (le.get("plans") or {})
                if not taking and not (x.get("wait_end") and link.error):
                    # 待つ間が終わった後と、終え方から外れたレーンは、未検出で
                    # 終わりをまだ記録していないレーン(戻ったら記録する)だけが実行の
                    # 途中である。応答のあるレーンは、監視がすぐ終わりを書く。その前に
                    # 開始し直してもよい(開始が前の実行の終わりを書く)
                    continue
                out[x["dev"]] = (((le or {}).get("since") or {}).get(x["dev"])
                                 or g.get("started_at") or 0)
        return out, errors

    def change(self, devs: list, mode: str, lap) -> dict:
        """周回の終え方を選ぶ。devs のレーンが属するグループの全レーンが対象である。

        lap は画面がメニューに出していた今の周で、いまの周と違えば何もしない
        (メニューの文言と違う動きをしない)。戻す項目は、名前が周を示さず、戻す
        先も周に依らないので比べない。選べるかは、いま読んだ状態で改めて決める
        (収集した状態は最大 1 秒古い)。

        続きを始めている途中(転送と開始の要求の間)は、予約を変えても続きには反映
        されないので選ばせない。ただし戻す項目は、始め終えるのを待ってから、始めた続きを
        戻す。先に停止したレーンを待つ間のメニューは戻す項目だけで(_held_revert)、
        待つ間の終わりに続きを始める途中が挟まることは、画面から見えないからである。
        """
        if mode not in lapend.MODES:
            return {"error": "周回の終え方が正しくありません"}
        end = time.monotonic() + self.RESTART_WAIT_S
        while True:
            got = self._change(devs, mode, lap)
            if got is not None:
                return got
            if mode != "revert" or time.monotonic() >= end:
                return {"error": _RESTARTING}
            time.sleep(0.05)

    def _change(self, devs: list, mode: str, lap) -> dict | None:
        """change の本体。続きを始めている途中のレーンがあり、まだ何も変えていなければ
        None を返す。"""
        ids = {link.registered_id for link in self.c._group_targets(devs)}
        by_id = self.c._by_id()
        with self.c._lock:
            groups = json.loads(json.dumps(self.c._state.get("groups") or []))
        # 動いているグループだけが対象である(「区切りで停止」の一括操作と同じ)。
        # 周回を終えるために全レーンが停止し、続きを始める前のグループも、1 回の
        # 実行の途中なので含める(待つ間は元の周回数に戻せる)。終わりを記録した
        # グループは、レーンがまだ動いていても除く。監視はその予約を進めない
        # (連動停止の後始末だけを続ける)ので、レーンは予約で止まったまま続きが
        # 始まらない
        gids = [g.get("id", "") for g in groups
                if any(x.get("dev") in ids for x in g.get("lanes") or [])
                and not self.c._closed(g)
                and (g.get("lap_end") or self.c._group_active(
                    [x for x in g.get("lanes") or [] if x.get("dev") in by_id],
                    by_id))]
        if not gids:
            if any(any(x.get("dev") in ids for x in g.get("lanes") or [])
                   and self.c._closed(g) for g in groups):
                # 連動停止を始めたグループ(menus と同じ文)
                return {"error": _IDLE}
            return {"error": "周回を終えるレーンが見つかりません"}
        # 続きの開始には操作権が必要である。停止するだけで始められなければ、実行が途中で
        # 終わる
        if any(by_id[x["dev"]].owned_by_other for g in groups
               if g.get("id", "") in gids for x in _taking_part(g)
               if x.get("dev") in by_id):
            return {"error": _OWNED}
        chosen, held = [], []
        for gid in gids:
            with self.c._lock:
                g = json.loads(json.dumps(self.c._group_by_id(gid) or {}))
                # 続きを始めている間(転送と開始の途中)は、予約を変えても続きには
                # 反映されない。記録と実行が一致しないので選ばせない(change)。装置の
                # 状態を取得する前に見る(待つ間に、装置へ毎回問い合わせない)
                restarting = any(x.get("dev") in self._restarting
                                 for x in g.get("lanes") or [])
            if restarting:
                return None
            # 未検出と、応答がないだけのレーンは、戻るまで状態を読めない。読みに行くと、
            # つなぎ直しの待ちの後に失敗する。読みに行く前に理由を示す(menus と同じ
            # 文で、画面の ▾ もこれを出す。終える項目にも元に戻す項目にも当てはまる
            # 言い方にする)。元に戻すことは、装置を読まずに決められる
            links = [by_id[x["dev"]] for x in _taking_part(g)
                     if x.get("dev") in by_id]
            if any(link.gone or link.error for link in links):
                if mode == "revert" and g.get("lap_end") and self._held_revert(g):
                    # 戻すことは装置を読まずに決められる(menus と同じ)
                    held.append(gid)
                    continue
                return {"error": _LOST if any(link.gone for link in links)
                        else _NO_REPLY}
            fresh = {}
            for x in _taking_part(g):
                link = by_id.get(x.get("dev"))
                if link is None:
                    continue
                try:
                    st = link.call(lambda c: c.status())
                    link.write_through(status=st)
                    fresh[x["dev"]] = st
                except (DeviceError, OSError):
                    name = link.cfg.get("name")
                    return {"error": f"{name} の状態を取得できません。"
                                     "もう一度選択してください。"}
            view = self._view(g, by_id, fresh)
            if view is None and g.get("lap_end") and mode == "revert" \
                    and self._held_revert(g):
                held.append(gid)
                continue
            # 理由は menus と同じ文で示す(開いたままのメニューを押したとき。画面の ▾
            # が出す文と同じになる)
            if view is None and self._why_no_view(g, by_id) == _UNREADABLE:
                return {"error": _UNREADABLE}
            if view is None and g.get("lap_end"):
                # menus と同じ文
                return {"error": _HOLDING if self._waiting_for_others(g, by_id)
                        else _RESTARTING}
            if view is None:
                return {"error": _IDLE}
            if view["after"]:
                return {"error": _AFTER_REVERT if mode == "revert" else _AFTER}
            if any((ln["st"].get("stop_at_mark") or 0) not in (0, lapend.MASK)
                   for ln in view["lanes"]):
                # 人が予約した止め方を、黙って置き換えない
                return {"error": _RESERVED}
            if mode != "revert" and view["lap"] != lap:
                # メニューの周を過ぎていれば「既に」、まだ着いていなければ「まだ」
                # (画面の見出しの周は先へ補間するので、装置より先のこともある)
                when = "既に" if isinstance(lap, int) and view["lap"] > lap else "まだ"
                return {"error": f"{when} {view['lap']} 周目です。"
                                 "メニューからもう一度選択してください。"}
            options = {o["mode"]: o for o in self._options(view)}
            if mode not in options:
                return {"error": "その終え方は、今は選択できません"}
            if options[mode].get("why"):
                return {"error": options[mode]["why"]}
            chosen.append((gid, view))
        # 待つ間に戻すのを先に行う。続きを始め出していれば、まだ何も変えていない
        # うちに分かる
        for n, gid in enumerate(held):
            if not self._revert_held(gid, by_id):
                if n == 0:
                    return None
                return {"error": _RESTARTING}
        for n, (gid, view) in enumerate(chosen):
            why = self._arm(gid, view, mode)
            if why == _RESTARTING and mode == "revert" and n == 0 and not held:
                return None     # まだ何も変えていない。始め終えるのを待って戻す
            if why:
                return {"error": why}
        return {"ok": True}

    @staticmethod
    def _held_revert(g: dict) -> list[dict]:
        """先に停止したレーンがほかのレーンを待つ間に選べる終え方。

        元の周回数に戻す項目だけである。戻すのをやめると、選び間違えたときに
        取り消す手段が「停止」しかなくなる(_switch_blocked と同じ考え)。ほかの
        終え方は、停止した位置が決まった後では今の周を改めて決められないので出さない。
        戻す先の周回数が今の予定と同じなら、項目はない(_options と同じ)。
        """
        plans = (g.get("lap_end") or {}).get("plans") or {}
        backs = [x["orig_laps"] for x in g.get("lanes") or [] if "orig_laps" in x]
        if not backs or all(list(plans.get(x["dev"]) or [])[:1] == [x["orig_laps"]]
                            for x in g.get("lanes") or [] if "orig_laps" in x):
            return []
        return [{"mode": "revert", "laps": _revert_laps(backs)}]

    def _revert_held(self, gid: str, by_id: dict) -> bool:
        """待っている間に、元の周回数に戻す。続きは停止した位置から元の周回数で始まる。

        動いているレーンの予約はそのままにする。全レーンが停止してから続きを始める
        のは、ほかの終え方と同じである(合流点の相手と周がずれないようにする)。続きを
        始めている途中なら戻さずに偽を返す(lap_end の確認から予約を書くまでの間に
        始まると、戻した記録と始めた続きが一致しない)。開始のメッセージも元の周回数の
        ものに戻す(_chosen_notes)。
        """
        with self.c._lock:
            g = json.loads(json.dumps(self.c._group_by_id(gid) or {}))
        # 変換はロックの外で行う。レーンの記録のメッセージと周回数は、続きを始めるまで
        # 変わらない(続きはグループを作り直す。下で同じグループかを確かめる)
        backs = [x for x in g.get("lanes") or [] if "orig_laps" in x]
        notes = self._chosen_notes(
            [{"dev": x["dev"], "lane": x,
              "art": self.c.project.sent.built(x.get("proc_id") or "", x["dev"])}
             for x in backs],
            {x["dev"]: (INFINITE if x["orig_laps"] is None else x["orig_laps"], True)
             for x in backs})
        with self.c._lock:
            cur = self.c._group_by_id(gid)
            if cur is None or not cur.get("lap_end"):
                return True
            if any(x.get("dev") in self._restarting for x in cur.get("lanes") or []):
                return False
            le = cur["lap_end"]
            le["mode"] = "revert"
            # 終え方から外したレーンについて保持した記録(left)は、選び直す前の計画の
            # ものである。捨てる(外したレーンは、予約を取り消しても計画に戻さず、
            # 元の周回数のまま動く)
            le.pop("left", None)
            lanes = [x for x in cur.get("lanes") or [] if "orig_laps" in x]
            for x in lanes:
                le.setdefault("plans", {})[x["dev"]] = [x["orig_laps"], True]
            le["notes"] = {**(le.get("notes") or {}), **notes}
            for x in lanes:
                if x["dev"] not in notes:
                    # 求められなかったレーンは、選んだ周回数のメッセージを残さない
                    le["notes"].pop(x["dev"], None)
            self.c._save_runstate()
        for x in lanes:
            link = by_id.get(x["dev"])
            if link is not None:
                self.c._log(link, "PC_LAPS", mode="revert", rule="",
                          laps=x["orig_laps"])
        return True

    def _arm(self, gid: str, view: dict, mode: str) -> str:
        """周回数を変える予約を置く。置けなければ、その理由の文を返す。

        選択待ちの途中のレーンは、予約を受け取った時点でその待ちで停止する。予約の時点で
        待っていた時間と、その待ちの区切りを残し、続きが同じ待ちから始まるときだけ
        時間切れから待った分を引く。スクリプトのブロックの待ちの途中の
        レーンには、まだ予約を送らない(停止すると、続きでスクリプトをもう一度実行
        する)。スクリプトが終わって選択を送るときに予約する(after_script)。
        変える前の周回数に戻すだけで、まだ始め直していなければ、予約を消すだけに
        する(始め直す理由がない)。

        各レーンを読んでから置くまでの間に、全レーンが予約で停止して続きを始めて
        いたら(_restarting)、何も送らずに拒否する。続きを動かし始めた装置へ予約を
        送ると、続きのグループには終え方がないので、誰も押していない「区切りで停止」で
        停止する。
        見るのは、装置へ送る前と、記録を置くのと同じ排他の中である。
        """
        k, span = view["lap"], view["span"]
        plans = {ln["dev"]: lapend.plan_of(mode, k, span, ln["lane"].get(
                     "orig_laps", ln["lane"].get("laps")))
                 for ln in view["lanes"]}
        if all(plans[ln["dev"]] == ln["running"] for ln in view["lanes"]):
            # 動いている手順のままに戻すだけである。予約を消し、選んだ終え方をやめる。
            # 1 台でも解除が送れなければ、記録を残して失敗を返す。記録を消すと、
            # 装置に残った予約で次の選択待ちか周の境で停止するのに、予約を揃え直す
            # 処理がない(記録が残っていれば、監視が全レーンへ予約を送り直す)
            if self._restarting_in(gid):
                return _RESTARTING
            failed = []
            for ln in view["lanes"]:
                link = ln["link"]
                try:
                    link.write_through(status=link.call(
                        lambda c: (c.stop_at_mark(release=True), c.status())[1]))
                except (DeviceError, OSError) as e:
                    failed.append(sentences(
                        f"{link.cfg.get('name')} の予約を取り消せません",
                        failure_text(e, link.cfg.get("host") or "")))
            if failed:
                return sentences("元の周回数に戻せません", *failed)
            if any(lapend.held(ln["link"].status or {}) for ln in view["lanes"]):
                # 読んでから解除が送られるまでに、予約で停止したレーンがある。
                # そのレーンは続きでしか動かせない。待つ間に戻すときと同じく、元の
                # 周回数の続きにする(解除したレーンの予約は、監視が送り直す。watch)。
                # 終え方を消すと、停止したレーンが誰も押していない「区切りで停止」の
                # トリガーになる
                return "" if self._revert_held(gid, self.c._by_id()) else _RESTARTING
            with self.c._lock:
                cur = self.c._group_by_id(gid)
                if cur is not None and any(x.get("dev") in self._restarting
                                           for x in cur.get("lanes") or []):
                    # 解除を送っている間に、全レーンが停止して続きを始めた
                    return _RESTARTING
                if cur is not None:
                    cur.pop("lap_end", None)
                    for x in cur.get("lanes") or []:
                        # 元の周回数で動いているなら、戻す先はもう必要ない
                        if lapend.plan_of("revert", k, span, x.get(
                                "orig_laps", x.get("laps"))) == plans.get(x["dev"]):
                            x.pop("orig_laps", None)
                    self.c._save_runstate()
            for ln in view["lanes"]:
                self.c._log(ln["link"], "PC_LAPS", mode=mode, rule=view["kind"],
                          span=view["span"], laps=ln["lane"].get("laps"))
            return ""
        script, waited, since = [], {}, {}
        now = time.time()
        for ln in view["lanes"]:
            link, st = ln["link"], ln["st"]
            since[ln["dev"]] = link.run_started_at
            got = (current_await(self.c.project, st, ln["dev"])
                   if st.get("awaiting") else None)
            if got and got[1].get("script"):
                script.append(ln["dev"])
            elif st.get("awaiting"):
                # 予約を受け取るとこの待ちで停止する。ここまでに待った時間と、どの待ち
                # か(最後に通った区切り)を残す。予約を送れずに先へ進み、別の
                # 待ちで停止したときは引かない
                lm = st.get("last_mark") or {}
                waited[ln["dev"]] = [max(0.0, now - (link.mark_at or now)),
                                     lm.get("index"), list(lm.get("counters") or [])]
        # 記録を先に置く。予約で停止したレーンを、監視が普通の止まり方として
        # 記録しないようにするためである
        stored = {dev: _stored_plan(plan) for dev, plan in plans.items()}
        with self.c._lock:
            cur = self.c._group_by_id(gid)
            if cur is None:
                return ""
            if any(x.get("dev") in self._restarting for x in cur.get("lanes") or []):
                return _RESTARTING
            old = cur.get("lap_end") or {}
            for x in cur.get("lanes") or []:
                if x.get("dev") not in plans:
                    continue          # 終え方から外れたレーン(_taking_part)
                x.setdefault("orig_laps", x.get("laps"))
                # 前に終え方をやめたときの待つ間の終わりは、この終え方には当てはまらない
                # (使うのは周回を終える予約で停止したときなので、残すとこの予約で
                # 停止したレーンに使われる)
                x.pop("wait_end", None)
            placed = {
                "mode": mode,
                "plans": json.loads(json.dumps(stored)),
                "script": script,
                "waited": {**(old.get("waited") or {}), **waited},
                "since": old.get("since") or since,
                "notes": {},
                # 周回数の変更の行の書き方(rejoin_if_ready が同じ行を書き直す)
                "rule": view["kind"], "span": view["span"]}
            cur["lap_end"] = placed
            self.c._save_runstate()
        for ln in view["lanes"]:
            if ln["dev"] in script:
                continue
            link = ln["link"]
            try:
                link.write_through(status=link.call(
                    lambda c: (c.stop_at_mark(lapend.MASK), c.status())[1]))
            except (DeviceError, OSError):
                pass              # 監視が送り直す(watch)
        # 開始のメッセージを、選んだ周回数で開始したときのものにする(画面へは選んだ
        # 時点で出す。Coupler.snapshot)。画面に出すだけなので、予約を送った後に
        # ロックの外で変換する(予約の前に変換すると、項目を確かめた位置をレーンが
        # 過ぎる間が延びる)。その間に予約が替わっていたら(戻した・停止した)、置かない
        notes = self._chosen_notes(view["lanes"], plans)
        with self.c._lock:
            cur = self.c._group_by_id(gid)
            if (cur is not None and cur.get("lap_end") is placed
                    and placed["mode"] == mode and placed["plans"] == stored):
                placed["notes"] = notes
                self.c._save_runstate()
        for ln in view["lanes"]:
            laps = plans[ln["dev"]][0]
            self.c._log(ln["link"], "PC_LAPS", mode=mode, rule=view["kind"],
                      span=view["span"], laps=None if laps == INFINITE else laps)
        return ""

    def _restarting_in(self, gid: str) -> bool:
        """そのグループのどれかのレーンの続きを始めている途中か。"""
        with self.c._lock:
            return any(x.get("dev") in self._restarting
                       for x in (self.c._group_by_id(gid) or {}).get("lanes") or [])

    def after_script(self, dev_id: str) -> int:
        """スクリプトのブロックの待ちを進めた直後に送る予約の mask。なければ 0。

        条件の監視(condwatch)が、スクリプトを実行し終えて選択肢 0 を選ばせるときに
        呼び、同じ接続で続けて予約を送る。後回しにした予約を、続きを実行し始める
        前に送るためである。後回しにした印(script)は、ここでは外さない。選択が
        送れなければ、次の周期に送り直す選択にも予約を付ける。印は、レーンが待ちを
        抜けたのを見て監視が外す(watch)。
        """
        with self.c._lock:
            for g in self.c._state.get("groups") or []:
                le = g.get("lap_end")
                if le and dev_id in (le.get("script") or []):
                    return lapend.MASK
        return 0

    def _set_script(self, gid: str, dev: str, on: bool) -> None:
        """そのレーンを、予約を後回しにするレーン(script)に入れるか外す。"""
        with self.c._lock:
            cur = self.c._group_by_id(gid)
            le = (cur or {}).get("lap_end")
            if not le:
                return
            script = le.setdefault("script", [])
            if on and dev not in script:
                script.append(dev)
            elif not on and dev in script:
                script.remove(dev)
            self.c._save_runstate()

    def _chosen_notes(self, lanes: list[dict], plans: dict) -> dict:
        """各レーンを、元の開始位置から新しい予定で開始したときの開始のメッセージ。

        lanes は {"dev", "lane"(グループのレーンの記録), "art"(動いている実行の送った
        手順のコピー)} の並び、plans は {装置 ID: (周回数(INFINITE を含む), 解決するか)}
        である。{装置 ID: メッセージ} を返す。求められないレーン(送った手順のコピーや
        開始した時点の定義が読めない、変換できない)は含めない。画面はそのレーンの記録の
        メッセージのまま出し、続きの開始も持ち越す(Coupler._continued_notes)。
        """
        out = {}
        for ln in lanes:
            art = ln.get("art")
            if art is None:
                continue
            got = self.c.notes_at_laps(ln["lane"], art, *plans[ln["dev"]])
            if got is not None:
                out[ln["dev"]] = got
        return out

    def take_resumed_wait(self, dev_id: str) -> bool:
        """そのレーンが、周回を終えるために停止した待ちから続けて、まだ通知の監視が
        見ていないか。見たことにして返す(notify が待ちの通知を出し直さない)。"""
        with self.c._lock:
            if dev_id in self._resumed_waits:
                self._resumed_waits.discard(dev_id)
                return True
            return False

    def ending(self, ids: set) -> set:
        """そのレーン(装置 ID)のうち、周回の終え方を選んで続きを始めるまでのグループ
        (lap_end)の、終え方に加わっているもの(_taking_part)。"""
        with self.c._lock:
            return {x.get("dev") for g in self.c._state.get("groups") or []
                    if g.get("lap_end") for x in _taking_part(g)
                    if x.get("dev") in ids}

    def stop_of(self, ids: set, stopped: str = "", mode: str = "") -> tuple[set, list]:
        """人がレーン ids に止め方 mode を押した(Coupler.stop_lanes)。止め方を送る前に
        呼ぶ。(PC がまだ停止したと読んでいないレーンの装置 ID(drop_of と同じ), 周回数を
        元に戻した行の並び(log_backs に渡す)) を返す。

        stopped は「停止」を押したときの止まり方で、「区切りで停止」なら空である。
        周回の終え方を選んだグループごとに、終え方に加わっている全レーンを押したか
        (グループの見出し、全レーンを選んだ一括操作)で分ける。
        - 全レーンなら、終え方をやめる(drop_of)。続きを始めている途中なら、中止の
          フラグを立てる(_restart は、始めている間のフラグを立てるのと同じ排他の中で
          予約がまだあるかを見るので、予約をやめてからフラグを立てるこの順なら取り
          こぼさない)
        - 一部のレーンなら、押したレーンだけを終え方から外す(_leave_locked)。残りの
          レーンは計画のまま続ける
        - 一部のレーンで、そのグループの続きを始めている途中なら、外さずに、終え方の
          記録(lap_end の late)に押した止め方を保存する。続きのグループの記録にその
          止め方を置き(Coupler._record_group)、続きを始めた後にそのレーンだけをその
          止め方で停止させる(_restart_with)。続きは始めた時点の計画で始まるので、外すと
          記録と実行が一致しない
        外すか保存するかは、_restart が始めている間のフラグを立てて計画のコピーを取る
        のと同じ排他の中で決める。分けると、その間に続きが始まり、外したレーンも続きを
        受ける。
        行は、止め方を送ってから書く(送れなかったら外したことを戻すので、書かない。
        undo_leave)。
        """
        by_id = self.c._by_id()
        at = time.time()
        whole: set = set()
        unread: set = set()
        backs: list = []
        with self.c._lock:
            changed = False
            for g in self.c._state.get("groups") or []:
                le = g.get("lap_end")
                if not le:
                    continue
                devs = {x.get("dev") for x in _taking_part(g)}
                hit = devs & ids
                if not hit:
                    continue
                if hit == devs:
                    whole |= hit
                elif devs & self._restarting:
                    late = le.setdefault("late", {})
                    for dev in hit:
                        if late.get(dev) != "immediate":
                            late[dev] = mode
                    changed = True
                else:
                    got_unread, got_backs = self._leave_locked(
                        g, hit, stopped, by_id, at)
                    unread |= got_unread
                    backs += got_backs
                    changed = True
            if changed:
                self.c._save_runstate()
        if whole:
            unread |= self.drop_of(whole, stopped=stopped)
            with self.c._lock:
                self._restart_cancel |= whole & self._restarting
        return unread, backs

    def wait_restart(self, ids: set) -> None:
        """レーン ids の一部だけを停止させるとき、そのレーンのグループの続きを始めている
        途中なら(転送と開始の要求の間)、始め終えるのを待つ(Coupler.stop_lanes が、
        止め方を送る前に呼ぶ)。最長 RESTART_WAIT_S 秒で待つのをやめる。

        始め終えた後は、レーンは続きのグループの新しい記録で動いているか、続きを始め
        られずに停止している。押した止め方はその記録に置く。待ちきれなかったときと、
        待ち終えた後にまた続きを始めたときの扱いは stop_of が決める。全レーンを
        押したときは待たない(中止のフラグで続きを始めさせない。stop_of)。
        """
        end = time.monotonic() + self.RESTART_WAIT_S
        while time.monotonic() < end:
            with self.c._lock:
                busy = False
                for g in self.c._state.get("groups") or []:
                    if not g.get("lap_end"):
                        continue
                    devs = {x.get("dev") for x in _taking_part(g)}
                    hit = devs & ids
                    if hit and hit != devs and devs & self._restarting:
                        busy = True
            if not busy:
                return
            time.sleep(0.05)

    def _leave_locked(self, g: dict, ids: set, stopped: str, by_id: dict,
                      at: float) -> tuple[set, list]:
        """グループ g のレーン ids を、周回の終え方から外す(stop_of。Coupler のロックの
        下で呼ぶ)。グループのほかのレーンは、終え方の計画のまま続ける。

        外したレーンは、続きが始まる前に終わるので、drop_of と同じく、記録の周回数(元の
        周回数)で書き、周回数が変わっていたら周回数を元に戻した行を書き(行は返し、
        止め方を送った後に書く)、レーンの記録に待つ間の終わり(wait_end。押した時刻と
        押した止め方)を置く。予約で止まって待っていたレーンの終わりは、それで書く
        (Coupler._note_run_end)。動いていたレーンは、押した止め方で停止する。外した
        レーンは待つレーンに数えず(holds_and_errors)、監視は続きを待たずにそのレーンの
        終わりを書く(Coupler._watch_group)。続きには加えず、続きのグループへ移す
        (_restart_with。グループは分かれない)。

        外すのと同じ排他の中で、レーンの記録に押した止め方(hand_stop)も置く。
        外した物は lap_end の left に保存する。止め方が送れなかったら戻し(undo_leave)、
        「区切りで停止」を取り消したら、続きが始まる前なら計画に戻す(ask_rejoin)。
        (PC がまだ停止したと読んでいないレーンの装置 ID, 周回数を元に戻した行) を返す。
        """
        le = g["lap_end"]
        plans = le.get("plans") or {}
        held = at + 0.001
        unread: set = set()
        backs: list = []
        for x in g.get("lanes") or []:
            dev = x.get("dev")
            if dev not in ids or dev not in plans:
                continue
            plan = plans.pop(dev)
            kept = {"plan": plan, "back": False, "how": stopped or "graceful",
                    "script": dev in (le.get("script") or [])}
            for key in ("waited", "notes", "since"):
                got = (le.get(key) or {}).pop(dev, None)
                if got is not None:
                    kept[key] = got
            if dev in (le.get("script") or []):
                le["script"].remove(dev)
            if plan[0] != x.get("laps") and not x.get("ended"):
                backs.append((dev, x.get("laps"), at))
                kept["back"] = True
            # 周回数は元のまま終わるので、戻す先はもう要らない(_held_revert)
            if "orig_laps" in x:
                kept["orig_laps"] = x.pop("orig_laps")
            if "wait_end" in x:
                kept["wait_end"] = x["wait_end"]
            # 押した止め方(hand_stop)を、外すのと同じ排他の中で置く。外した後、置く前に
            # そのレーンが周回を終える予約で停止すると、監視は外したレーンが
            # 予約と違う止まり方で停止したとみなし、終え方をやめてしまう(watch)。
            # 強い方を残すのは Coupler._mark_hand_stop と同じ。前の値は保存し、
            # 送れなかったら戻す
            kept["hand_before"] = x.get("hand_stop")
            if x.get("hand_stop") != "immediate":
                x["hand_stop"] = "immediate" if stopped else "graceful"
            le.setdefault("left", {})[dev] = kept
            we = {"at": held, "stop": stopped, "by": None}
            link = by_id.get(dev)
            st = getattr(link, "status", None) or {}
            busy = bool(st.get("running") or st.get("awaiting"))
            if lapend.held(st):
                # 予約で止まって待っていたレーンの停止への変化は、連動停止の
                # トリガーにしない(drop_of と同じ)
                self.c._was_busy[dev] = False
            elif (busy and link is not None and not link.error
                  and st.get("stop_at_mark") == lapend.MASK):
                unread.add(dev)
            elif busy and link is not None and link.error:
                # 戻って最初に読めた状態で見る(held_on_return)。押した止め方は
                # 戻ったら送るので、予約の解除は送らない
                we.update(unread=True, release=False)
            x["wait_end"] = we
        return unread, backs

    def log_backs(self, backs: list) -> None:
        """終え方から外したレーンの、周回数を元に戻した行を書く(stop_of が返した
        (装置 ID, 周回数, 押した時刻) の並び)。行の時刻は押した時刻である(終わりの行は
        その後に並ぶ)。"""
        by_id = self.c._by_id()
        for dev, laps, at in backs:
            link = by_id.get(dev)
            if link is not None:
                self.c._log(link, "PC_LAPS", mode="revert", rule="", laps=laps, at=at)

    def undo_leave(self, ids: set) -> None:
        """止め方が送れず、戻したときに送ることもしなかったレーン ids
        (Coupler.stop_lanes。送る途中で予期しない例外が出て、まだ送っていないレーンも)
        を、終え方から外す前に戻す(stop_of)。装置は押した止め方で停止しないので、外した
        ままにすると、予約で停止したときに、予約と違う止まり方とみなして終え方を
        やめてしまう。外すときに置いた押した止め方(hand_stop)も押す前の値に戻す。
        周回数を元に戻した行は、まだ書いていない。続きを始めている途中に保存した止め方
        (late)も外す。
        """
        with self.c._lock:
            changed = False
            for g in self.c._state.get("groups") or []:
                le = g.get("lap_end")
                if not le:
                    continue
                for dev in ids & set(le.get("late") or {}):
                    del le["late"][dev]
                    changed = True
                left = le.get("left") or {}
                for x in g.get("lanes") or []:
                    dev = x.get("dev")
                    kept = left.get(dev)
                    if dev not in ids or kept is None:
                        continue
                    del left[dev]
                    self._put_back(le, x, kept)
                    if "wait_end" in kept:
                        x["wait_end"] = kept["wait_end"]
                    if "hand_before" in kept:
                        # 外すときに置いた押した止め方を、押す前の値に戻す
                        if kept["hand_before"] is None:
                            x.pop("hand_stop", None)
                        else:
                            x["hand_stop"] = kept["hand_before"]
                    changed = True
            if changed:
                self.c._save_runstate()

    @staticmethod
    def _put_back(le: dict, x: dict, kept: dict) -> None:
        """外したレーンの記録 x を、保存した kept(_leave_locked)から計画に戻す(ロックの
        下で呼ぶ)。待つ間の終わりは外す。"""
        dev = x.get("dev")
        le.setdefault("plans", {})[dev] = kept["plan"]
        for key in ("waited", "notes", "since"):
            if key in kept:
                le.setdefault(key, {})[dev] = kept[key]
        if kept.get("script"):
            le.setdefault("script", []).append(dev)
        if "orig_laps" in kept:
            x["orig_laps"] = kept["orig_laps"]
        x.pop("wait_end", None)

    def ask_rejoin(self, ids: set) -> None:
        """人がレーン ids の「予約を取り消す」を押し、取り消しが送られた
        (Coupler.stop_lanes)。待つ間に「区切りで停止」で終え方から外したレーン
        (_leave_locked が lap_end の left に保存した)なら、計画に戻す印(back_in)を
        付ける。戻すのは監視で、取り消した後の状態を読んでから戻す(rejoin_if_ready)。
        取り消しの前に読んだ状態(区切りで停止の予約がある)で計画に加えると、重なった
        区切りで停止とみなして終え方をやめてしまう。

        続きが始まった後(終え方がない、続きを始めている途中)、終わりを書いたレーン、
        「停止」も押したレーン(記録の hand_stop が残る)は戻さない。
        """
        with self.c._lock:
            changed = False
            for g in self.c._state.get("groups") or []:
                le = g.get("lap_end")
                left = (le or {}).get("left") or {}
                if not left or any(x.get("dev") in self._restarting
                                   for x in g.get("lanes") or []):
                    continue
                for x in g.get("lanes") or []:
                    dev = x.get("dev")
                    if (dev in ids and dev in left and not x.get("ended")
                            and not x.get("hand_stop")
                            and left[dev].get("how") == "graceful"):
                        left[dev]["back_in"] = True
                        changed = True
            if changed:
                self.c._save_runstate()

    def rejoin_if_ready(self, gid: str, link, st: dict) -> bool:
        """終え方から外したレーンに計画に戻す印(ask_rejoin)があり、状態 st(取り消した
        後に読んだもの)で、まだ動いていて区切りで停止の予約がないか、周の境で予約で
        停止していれば、計画に戻す(押す前と同じ状態)。戻したら真を返す(watch)。

        計画、待った時間、メッセージ、開始の時刻、スクリプトの待ちの印、戻す先の周回数を
        元に戻し、待つ間の終わりを外す。周回数を元に戻した行を書いていたら、選んだ
        周回数の行をもう一度書く。周回を終える予約は監視が送り直す(watch)。
        """
        dev = link.registered_id
        moving = bool(st.get("running") or st.get("awaiting"))
        mask = st.get("stop_at_mark") or 0
        if not ((moving and mask in (0, lapend.MASK)) or lapend.held(st)):
            return False
        with self.c._lock:
            g = self.c._group_by_id(gid) or {}
            le = g.get("lap_end")
            left = (le or {}).get("left") or {}
            kept = left.get(dev)
            x = next((x for x in g.get("lanes") or [] if x.get("dev") == dev), None)
            if (not kept or not kept.get("back_in") or x is None or x.get("ended")
                    or x.get("hand_stop")):
                return False
            del left[dev]
            self._put_back(le, x, kept)
            said = {"mode": le.get("mode") or "", "rule": le.get("rule") or "",
                    "span": le.get("span") or 0}
            self.c._save_runstate()
        if kept.get("back"):
            self.c._log(link, "PC_LAPS", **said, laps=kept["plan"][0])
        return True

    def drop_of(self, ids: set, release: bool = False, stopped: str = "",
                cause: tuple[str, str] | None = None) -> set:
        """そのレーンが属するグループの、周回数を変える予約をやめる(人が終え方に加わって
        いる全レーンを停止させた、ほかのレーンが予約と違う止まり方で停止した)。終え方に
        加わっているレーン(_taking_part)の記録に待つ間の終わり(wait_end)を置き、PC が
        まだ停止したと読んでいないレーンの装置 ID を返す。

        stopped は人が「停止」を押したときの止まり方で、ほかは空である。cause は予約を
        やめさせたレーンの [装置 ID, 止まり方] で、人が停止させたときは None である。
        release が真なら、まだ動いていて予約(MASK)を持つレーンに、予約の解除を送る。

        wait_end は、待つのをやめさせた出来事で、待っていたレーンの終わりをどう書くか
        を、出来事が起きた時点で決めて保存したものである。使うのは、そのレーンが周回を
        終える予約で止まっていたとき(lapend.held)だけで、終わりを書くときに
        装置の状態で決める(Coupler._note_run_end)。PC がまだ読んでいなかったレーン、
        応答がなかったレーンも、後で読めた状態で同じに決まる。予約が解除されて動き続けた
        レーンは、別の止め方で停止するので、wait_end を使わない。wait_end の欄は次の
        とおりである。

        - at: 出来事の時刻(人が押した時刻、ほかのレーンが停止した時刻)。続きを待つ間は
          1 回の実行の途中なので、実行が終わったのはその時刻である(装置が停止した
          時刻にすると、終わりの行が原因の行より前に並ぶ)
        - stop: 人が押した止め方の止まり方(「停止」なら stopped)。待っていたレーンの
          終わりをその止まり方で記録する。装置の止まり方(区切りで停止)のまま書くと、
          誰も押していない「区切りで停止」とみなされる
        - by: 予約をやめさせたレーン(watch)の [装置 ID, 止まり方]。予約と違う止まり方で
          停止したレーン(装置のボタンの長押し、別のツールからの停止、異常停止、完了)
          と、人が別のツールか装置のボタンの短押しで「区切りで停止」を重ねたレーン
          (止まり方は区切りで停止)である。待っていたレーンの終わりを、通常の連動
          停止と同じくそのレーンに連動して停止したと記録する(「2P の異常停止に連動
          して停止」)。
          区切りで停止を重ねたレーンが停止するのは待たない(周回数が無限なら何時間も先
          で、その間に待っていたレーンを開始し直すと、古い実行の終わりが後から書かれる)
        - unread・release: 応答がなかったレーン(下の、応答がないレーンを扱う所)

        終わりの行と前回の実行の周回数は、止まり方によらず、レーンの記録の周回数(元の
        周回数)で書く。選んだ周回数で動くのは続きからで、続きが始まらなければ、どの
        レーンも元の周回数の手順のまま終わる。周回数が変わっていたレーンには、元に
        戻したことを記録に書く(PC_LAPS の revert)。

        続きの RUN を送り始めた後(begin_runs)に止め方が押されたときは、続きはもう
        始まっているので、その行を書かずに保持する。終わりは続きのレーンの記録が持つ
        選んだ周回数で書く。保持した行は、どの装置も続きを始めなかったと分かったときに
        だけ書く(_end_cancelled)。
        """
        by_id = self.c._by_id()
        to_release = []
        unread: set = set()
        at = time.time()
        cst = getattr(by_id.get(cause[0]), "status", None) or {} if cause else {}
        # 予約で止まって待つ間に装置のボタンが押されたレーン(stop_by_press)。出来事は
        # 押したことで、停止したのはその前である
        press = cause is not None and lapend.held(cst) and bool(lapend.pressed(cst))
        if (cause is not None and not (cst.get("running") or cst.get("awaiting"))
                and not press and cst.get("stop_reason")):
            # 停止したレーンが停止した時刻(収集が見積もった時刻。記録の終わりの行と
            # 同じ)。重なった「区切りで停止」で、PC が読む前に区切りで停止していたときも
            # 同じ。止まり方の分からないレーン(未検出の間に装置が起動し直した)は、
            # 停止した時刻も分からない。見積もりは未検出になる前の実行のもの(周の境で
            # 停止した時刻など)なので使わず、PC がそれを知った今の時刻にする
            at = getattr(by_id.get(cause[0]), "run_ended_at", 0.0) or at
        # 周回数が変わっていたレーン。元に戻したことを記録に書く(周回数の変更の行だけが
        # 残ると、読む人は変わったままと読む)。見出しと周回数の欄も元の周回数に戻る
        backs = []
        # 待っていたレーンの終わりの時刻。周回数を元に戻した行の後に並べる
        held = at + 0.001
        with self.c._lock:
            changed = False
            for g in self.c._state.get("groups") or []:
                if g.get("lap_end") and any(x.get("dev") in ids
                                            for x in g.get("lanes") or []):
                    plans = g["lap_end"].get("plans") or {}
                    # 先に終え方から外したレーン(_leave)は、もう外した時に終わりの
                    # 書き方を決めてある。ほかのレーンの出来事で書き換えない
                    taking = _taking_part(g)
                    g.pop("lap_end", None)
                    changed = True
                    rows = []
                    for x in taking:
                        we = {"at": held, "stop": stopped,
                              "by": list(cause) if cause is not None else None}
                        if cause is not None and x.get("dev") == cause[0]:
                            # 原因のレーン自身は、自分に連動したとは書かない。ボタンを
                            # 押したレーンは、押したボタンの止まり方で終わる
                            we["by"] = None
                            if press:
                                we["stop"] = cause[1]
                        plan = plans.get(x.get("dev"))
                        if (plan is not None and plan[0] != x.get("laps")
                                and not x.get("ended")):
                            # 終わりを記録したレーン(続きを始めるときに、後ろに何もなく
                            # 完了と書いたレーン)は、選んだ周回数で終えている。終わりの
                            # 行の後に、周回数を戻した行を書かない
                            rows.append((x["dev"], x.get("laps")))
                        link = by_id.get(x.get("dev"))
                        st = getattr(link, "status", None) or {}
                        busy = bool(st.get("running") or st.get("awaiting"))
                        if lapend.held(st):
                            # 予約で止まって待っていたレーンは、停止への変化を
                            # 連動停止のトリガーにしない。監視は予約の間その変化を
                            # 見ていないので、やめた次の周期に「いま停止した」と
                            # みなし、本当に予約をやめさせたレーン(異常停止など)より
                            # 先にトリガーにしてしまう。そのレーンの変化は残す。
                            # 残さないと、どのレーンもトリガーにならず、まだ動いている
                            # レーンが連動して停止しない
                            self.c._was_busy[x.get("dev")] = False
                        elif (busy and not link.error
                              and st.get("stop_at_mark") == lapend.MASK):
                            # PC がまだ停止したと読んでいないレーン(予約を持って
                            # 動いていると見えている)も、もう予約で止まっている
                            # かもしれない(収集は毎秒読む)。装置 ID を返す。予約の
                            # 解除や人の止め方を送った応答で、予約で止まって
                            # いたと分かったら、トリガーにしない(held_if_stopped)。
                            # release なら予約の解除を送る。装置に残すと、その
                            # レーンは連動停止の対象から外れ(予約のあるレーンには
                            # 区切りの止め方を送らない)、選択待ちか周の境まで進んで、
                            # 誰も押していない止め方で停止する。人が止め方を指定して
                            # 停止させるとき(stop_lanes)は、その止め方が予約を置き換える
                            # ので送らない
                            unread.add(x.get("dev"))
                            if release:
                                to_release.append(link)
                        elif busy and link.error:
                            # 応答がない(未検出など)まま動いていると見えている
                            # レーンには、何も送れない。装置は予約を持ったまま周の
                            # 境で停止しうるので、unread を付け、戻って最初に読めた
                            # 状態で見る(held_on_return)。release なら、戻ったときに
                            # まだ動いていれば予約の解除を送る
                            we.update(unread=True, release=release)
                        x["wait_end"] = we
                    if g.get("id") in self._runs_begun:
                        self._backs_kept[g["id"]] = (at, rows)
                    else:
                        backs += rows
            if changed:
                self.c._save_runstate()
        self._log_backs(backs, at, by_id)
        for link in to_release:
            try:
                link.write_through(status=link.call(
                    lambda c: (c.stop_at_mark(release=True), c.status())[1]))
            except (DeviceError, OSError):
                continue    # 予約の所で停止し、待っていたレーンと同じに記録される
            self.held_if_stopped(link)
        return unread

    def _log_backs(self, rows: list, at: float, by_id: dict) -> None:
        """周回数を元に戻した行を書く。rows は (装置 ID, 戻した周回数) の並び、at は行の
        時刻(終え方をやめさせた出来事の時刻)。"""
        for dev, laps in rows:
            link = by_id.get(dev)
            if link is not None:
                self.c._log(link, "PC_LAPS", mode="revert", rule="", laps=laps, at=at)

    def _end_restarted(self, gid: str, link) -> None:
        """周回を終えるのを待つ間に起動し直したレーン link(止まり方がない)の終わりを、
        PC がそれを知った今の時刻で書く(watch)。装置は手順の途中も停止した所も持たない
        ので、続きは実行しない。監視の確定(1.5 秒)を待つと、待っていたレーンの終わりの
        行が先に並ぶ。

        続きは始まらないので、周回数は、待っていたレーンと同じく元の周回数で書く
        (drop_of)。周回数が変わっていたら、元に戻した行を終わりの行の前に書く(終わりを
        ここで記録するので、この後の drop_of はこのレーンに書かない)。未検出になる前に
        周回を終える予約で止まっていたなら、停止した所と終えた周はその状態で
        書き、最後の周の長さは装置が停止した時刻までとする。止まり方は分からないので、
        止まり方の分からない終わりのままにする。
        """
        dev = link.registered_id
        now = time.time()
        # 終わりの時刻。周回数を元に戻した行の後に並べる(drop_of と同じ)
        end = now + 0.001
        with self.c._lock:
            g = self.c._group_by_id(gid) or {}
            plan = ((g.get("lap_end") or {}).get("plans") or {}).get(dev)
            x = next((dict(x) for x in g.get("lanes") or [] if x.get("dev") == dev),
                     None)
        if x is None or not self.c._mark_lane_ended(gid, dev):
            return
        if plan is not None:
            if plan[0] != x.get("laps"):
                self.c._log(link, "PC_LAPS", mode="revert", rule="",
                            laps=x.get("laps"), at=now)
            x["wait_end"] = {"at": end}
        lost = getattr(link, "status_at_loss", None) or {}
        if lapend.held(lost):
            self.c._note_run_end(link, x, at=getattr(link, "run_ended_at", 0.0) or now,
                                 status=lost, restarted=True)
        else:
            self.c._note_run_end(link, x, at=end)

    def stop_by_press(self, gid: str, link, links: list, why: str) -> None:
        """予約で止まって待つ間に装置のボタンが押されたレーン link を原因に、待つ間を
        終える(watch)。

        そのレーンの終わりは押したボタンの止まり方(短押しは区切りで停止、長押しは
        装置のボタンでの停止)で、時刻は PC がそれを読んだ時刻である(装置は押した時刻を
        返さない)。ほかのレーンは、そのレーンのその止まり方に連動して停止したと記録し、
        まだ動いているレーンには連動停止を送る(短押しは次の区切りで、長押しはその場で)。
        周回数は、どちらの押し方でも元の周回数で書く(drop_of)。停止への変化が
        ないので、監視の連動停止は始まらない。ここで始める。why は watch が見たボタンの
        止まり方である(読み直さない)。
        """
        from .coupler import _LINKED_MODE, _LINKED_WHY
        dev = link.registered_id
        self.drop_of({dev}, release=True, cause=(dev, why))
        self.c._linked_stop(gid, links, dev, _LINKED_WHY[why],
                            mode=_LINKED_MODE[why], reason=why)

    def held_if_stopped(self, link) -> None:
        """予約をやめた時点で PC がまだ停止したと読んでいなかったレーン(drop_of が
        返す)が、送った操作の応答で、予約で止まっていたと分かったら、待って
        いたレーンと同じく、停止への変化を連動停止のトリガーにしない(終わりの書き方は
        wait_end が持つ)。"""
        if lapend.held(link.status or {}):
            with self.c._lock:
                self.c._was_busy[link.registered_id] = False

    def held_on_return(self, gid: str, link) -> None:
        """予約をやめた時点で応答がなかったレーン(drop_of が wait_end に unread を
        付けた)の状態を、戻って最初に読めたときに見る。予約で止まっていたら、
        待っていたレーンと同じく、停止への変化を連動停止のトリガーにしない(終わりの
        書き方は wait_end が持つ)。見たら unread をクリアする。状態は、戻ったときに送る
        止め方を送る前に読むこと。

        監視が予約をやめた(drop_of の release)のに、応答がなくて予約の解除を送れな
        かったレーンが動いて戻ったら、装置に残った周回を終える予約を解除する。残すと、
        そのレーンは連動停止の対象から外れ(予約のあるレーンには区切りの止め方を送ら
        ない)、次の選択待ちか周の境で、誰も押していない「区切りで停止」として停止する。
        解除できなければ unread を残し、次の周期に送り直す。人の止め方でやめたときは、
        戻ったときに送るその止め方が予約を置き換える。
        """
        st = link.status or {}
        if link.error or not st:
            return
        with self.c._lock:
            x = next((x for x in (self.c._group_by_id(gid) or {}).get("lanes") or []
                      if x.get("dev") == link.registered_id), None)
            we = (x or {}).get("wait_end") or {}
            if not we.get("unread"):
                return
            release = (bool(we.get("release"))
                       and bool(st.get("running") or st.get("awaiting"))
                       and st.get("stop_at_mark") == lapend.MASK)
        if release:
            try:
                link.write_through(status=link.call(
                    lambda c: (c.stop_at_mark(release=True), c.status())[1]))
            except (DeviceError, OSError):
                return
        with self.c._lock:
            if lapend.held(link.status or {}):
                self.c._was_busy[link.registered_id] = False
            x = next((x for x in (self.c._group_by_id(gid) or {}).get("lanes") or []
                      if x.get("dev") == link.registered_id), None)
            we = (x or {}).get("wait_end")
            if we:
                we.pop("unread", None)
                we.pop("release", None)
                self.c._save_runstate()

    def watch(self, gid: str, g: dict, links: list) -> bool:
        """周回数を変える予約を 1 周期進める。進めている間は True を返す。予約で停止して
        待つレーンの装置のボタンで予約をやめたときも、連動停止をここで始めたので True を
        返す(この周期の監視の残りを飛ばす)。

        全レーンが予約で停止したら、続きを始める。予約と違う止まり方をしたレーンが
        あれば(人が停止させた・異常・完了)、予約をやめて False を返す。普通の止まり方と
        して記録と連動停止を行う。人が「区切りで停止」で予約を上書きしたときも
        やめる(装置が返す予約の mask が変わる)。物理ボタンの短押しで上書きされ、PC が
        それを読む前に選択待ちか周の境で停止したときも、停止させた予約(stopped_mask)が
        この予約でないので、同じにやめる。

        未検出のレーン(応答のないレーン。応答がないだけのレーンがいる周期は、監視が
        ここへ来る前に何も決めずに戻る)があれば、そのレーンが戻るまで続きを始めない。
        ほかのレーンの出来事は、戻るのを待たずに見る。待つと、その間にほかのレーンが
        停止しても連動停止が送られず、重なった「区切りで停止」で停止したレーンも、戻った
        ときには予約で停止したのと区別できずに続きを始める。
        """
        from .coupler import _LINKED_MODE, _MODE_ORDER
        # 状態を先に読み、終え方と押した止め方はその後の今の記録で見る。レーンの止め方
        # は、そのレーンを終え方から外して(stop_of)、記録に止め方を置いてから送るので、
        # 押した止め方で停止した状態を読んだなら、記録はもう外れている。監視が周期の
        # 初めに取ったコピーで見ると、外れたばかりのレーンの停止を、終え方に加わった
        # レーンの予約と違う止まり方とみなし、終え方をやめてしまう
        sts = {link.registered_id: (link.status or {}) for link in links}
        le = self.live(gid)
        if le is None:
            return False          # その間に終え方がやめになった
        moving = []
        lost = False
        rejoined = False
        # 予約をやめさせる出来事 (止まり方, 種類, リンク)。同じ周期に 2 つ以上見えたら、
        # 連動停止(Coupler._stops_seen)と同じく、止め方の強いもの(その場で、次の
        # 区切りで、周の終わりでの順)を原因にする。並びの先で決めると、短押しと異常
        # 停止を一度に見たとき、残りが区切りまで動く
        events = []
        plans = le.get("plans") or {}
        # 押した止め方は、状態を読んだ後の今の記録で見る(Coupler._stops_seen と同じ)
        lane_of = self.c.lane_records(gid)
        for link in links:
            dev = link.registered_id
            st = sts[dev]
            if dev not in plans:
                # 終え方から外れたレーン(_leave)。待たず、予約も送らない。押した止め方
                # で停止したのでなく、装置が停止させた(異常停止、装置のボタンの長押し
                # など)なら、ほかのレーンと同じく予約をやめさせる出来事にする(装置の
                # ボタンと異常停止は、残りのレーンも停止させる)
                if link.error:
                    continue
                if self.rejoin_if_ready(gid, link, st):
                    # 予約を取り消したので計画に戻した。この周期は続きを始めない(次の
                    # 周期に、戻したレーンも待つ)
                    rejoined = True
                    continue
                if st.get("running") or st.get("awaiting"):
                    with self.c._lock:
                        self.c._was_busy[dev] = True
                    continue
                why = st.get("stop_reason") or ""
                if not self.c._was_busy.get(dev):
                    continue
                if self.c._by_hand(lane_of.get(dev) or {}, why):
                    # 押した止め方で停止した。この変化はトリガーにしない
                    with self.c._lock:
                        self.c._was_busy[dev] = False
                    continue
                events.append((why, "stopped", link))
                continue
            if link.error:
                lost = True       # 戻るまで続きを始めない(装置は予約で停止する)
                continue
            if st.get("running") or st.get("awaiting"):
                # 予約の間は _watch_active を呼ばないので、動いていることをここで
                # 覚える。覚えないと、サーバーを起動し直した後の予約の間に 1 台が
                # 異常停止しても、停止への変化が見つからず連動停止しない
                with self.c._lock:
                    self.c._was_busy[dev] = True
                mask = st.get("stop_at_mark") or 0
                if mask and mask != lapend.MASK:
                    # 人が「区切りで停止」を重ねた。待っていたレーンは、このレーンの
                    # 区切りで停止に連動したと記録する
                    events.append((proto.STOP_REASON_AT_MARK, "overlay", link))
                    continue
                got = (current_await(self.c.project, st, dev)
                       if st.get("awaiting") else None)
                in_script = bool(got and got[1].get("script"))
                listed = dev in (le.get("script") or [])
                if in_script != listed:
                    # スクリプトの待ちに入ったレーンは、終わるまで予約を後回しに
                    # する。後回しにしたレーンが待ちを抜けたら(スクリプトの後の
                    # 選択が送られたか、選択を送ったのが後回しにすると決めるのと
                    # 重なった)、印を外す。予約が送られていなければ、下で送る
                    self._set_script(gid, dev, in_script)
                if not mask and not in_script:
                    # 予約が送られていない(送れなかった)。送り直す
                    try:
                        link.write_through(status=link.call(
                            lambda c: (c.stop_at_mark(lapend.MASK),
                                       c.status())[1]))
                    except (DeviceError, OSError):
                        pass
                moving.append(dev)
            elif not lapend.held(st):
                # 予約と違う止まり方で停止したか、人の「区切りで停止」(装置のボタンの
                # 短押しなど)が予約を上書きした後に、PC がそれを読む前に区切りで停止した
                # (停止させた予約が周回を終える予約でない)。待っていたレーンは、この
                # レーンに連動して停止したと記録する。止まり方がないのは、未検出の間など
                # に装置が起動し直したときで、すぐ停止したものとして比べる
                events.append((st.get("stop_reason") or "", "stopped", link))
            elif lapend.pressed(st):
                # 予約で止まって待つ間に、そのレーンの装置のボタンが押された。装置は
                # 停止していて何もしないが、押した人はグループを停止させるつもりである
                # (短押しは区切りで停止、長押しは停止)
                events.append((lapend.PRESS_REASON[lapend.pressed(st)], "press", link))
        if events:
            why, kind, link = min(events, key=lambda e: _MODE_ORDER.index(
                _LINKED_MODE.get(e[0], "immediate")))
            dev = link.registered_id
            if kind == "press":
                # そのレーンを押したボタンで停止したと記録し、ほかのレーンをそれに
                # 連動して停止させる。停止への変化がないので、連動停止はここで始め、
                # この周期の監視の残りは飛ばす(残りは、連動停止で停止させたばかりの
                # レーンを新しいトリガーとみなす)
                self.stop_by_press(gid, link, links, why)
                return True
            if kind == "stopped" and not why:
                # 起動し直したレーンの終わりを先に書く(待っていたレーンの終わりは、
                # それより後に並ぶ)
                self._end_restarted(gid, link)
            self.drop_of({dev}, release=True, cause=(dev, why))
            return False
        if moving or lost or rejoined:
            return True
        self._restart(gid, [link for link in links if link.registered_id in plans])
        return True

    def _restart(self, gid: str, links: list) -> None:
        """予約で停止した全レーンを、停止した位置から新しい周回数で始める。

        周の境(次の周の先頭)で停止し、その周が新しい周回数を超えるレーンは、
        周回数のくり返しの直後から始める。後ろに何もなければ、そのレーンは
        そこで終わりである(完了として記録し、続きのグループに残す)。周回数の
        くり返しを抜けた所で停止し、新しい周回数がまだ先の周まであるレーン
        (周回数を戻した)は、次の周の先頭から始める。予約の時点で選択待ちの
        途中だったレーンは、そこまでに待った時間を続きの時間切れから引く。
        始められなければ、予約をやめてレーンに理由を残す(停止した所の終わりは
        普通に記録され、停止した位置から始め直せる)。

        監視が終え方を読んでからここまでの間に、人の止め方で終え方がやめになって
        いたら(lap_end がない)、何もしない。続きを始めると、人が停止させた後で、
        停止させるまで終わらない実行が動き出す。終え方がまだあるかは、始めている間の
        フラグを立てるのと同じ排他の中で見る(停止は終え方をやめてから中止のフラグを
        立てるので、どちらの順でも取りこぼさない。Coupler.stop_lanes)。
        """
        with self.c._lock:
            cur = self.c._group_by_id(gid)
            if cur is None or not cur.get("lap_end"):
                return
            g = json.loads(json.dumps(cur))
            devs = {x.get("dev") for x in _taking_part(g)}
            # 始めている間のフラグ。停止は中止のフラグを立て、予約の変更は拒否する
            self._restarting |= devs
        started = False
        try:
            started = self._restart_with(gid, g, links)
        finally:
            with self.c._lock:
                self._restarting -= devs
                self._restart_cancel -= devs
                self._runs_begun.discard(gid)
                # 続きが始まったか、拒否されたときは、保持した行を書かない(拒否された
                # 実行は「周回の終え方を変更できずに停止」と書く。_failed)
                self._backs_kept.pop(gid, None)
                if not started:
                    # 続きを始めなかった(始められない、中止した、ボタンが押された、
                    # 開始を拒否された、始めてすぐ停止した)。続きのために覚えた「同じ
                    # 待ちから続ける」の印をクリアする。残すと、次の実行がその装置の
                    # 待ちから始まったときに、通知の監視がその待ちを知らせない
                    self._resumed_waits -= devs

    def _restart_cancelled(self, gid: str, devs) -> bool:
        """続きを始めている間に、そのレーンの停止が押されたか。続きの開始の要求を送る
        前は、終え方がやめになったこと(lap_end がない)でも分かる(中止のフラグは
        終え方をやめた後に立つ)。"""
        with self.c._lock:
            return self._cancelled(gid, devs)

    def _cancelled(self, gid: str, devs) -> bool:
        """_restart_cancelled の中身(Coupler のロックの下で呼ぶ)。"""
        if self._restart_cancel & set(devs):
            return True
        cur = self.c._group_by_id(gid)
        return cur is not None and not cur.get("lap_end")

    def begin_runs(self, gid: str, devs) -> bool:
        """続きの RUN を送り始めてよいか(Coupler._send_runs が、最初の RUN の前に呼ぶ)。
        止め方がもう押されていたら偽を返し、RUN は送らない。

        真を返した時点から、そのグループの続きは始まったものとして扱う。この後に
        押された止め方は、続きが始まった後の止め方で、終わりは選んだ周回数で書き、
        周回数を元に戻した行は書かない(drop_of)。RUN を 1 台ずつ送っている途中に
        押されても、残りのレーンにも RUN を送ってから全レーンを停止させるので
        (_restart_with)、どのレーンも同じ周回数で書く(グループは 1 つの実行で、
        レーンごとに書き方を分けない)。

        止め方が押されたかを見るのと、始まったと印を付けるのは、同じ排他の中で行う。
        分けると、その間に押された止め方が、戻した行を書いた後に続きが始まる。
        """
        with self.c._lock:
            if self._cancelled(gid, devs):
                return False
            self._runs_begun.add(gid)
            return True

    def still_held(self, links: list) -> bool:
        """続きを始める直前に、レーンの装置の状態を読み直し、どのレーンもまだ周回を
        終える予約で停止していて、停止した後にボタンが押されていないかを返す。

        監視が見た状態は、収集が最大 1 秒前に読んだものである。その後に押されたボタンは
        続きの RUN で消える(装置は次の実行で空に戻す)ので、始める前に読み直す。押されて
        いたら始めず、次の周期の監視(watch)がそのレーンを原因に待つ間を終える。読めない
        レーンがあっても始めない(監視が戻るのを待つ)。
        """
        for link in links:
            try:
                st = link.call(lambda c: c.status())
            except (DeviceError, OSError):
                return False
            link.write_through(status=st)
            if not lapend.held(st) or lapend.pressed(st):
                return False
        return True

    def _restart_with(self, gid: str, g: dict, links: list) -> bool:
        """_restart の本体。g は始めた時点のグループのコピー。続きを始めたら真を
        返す。"""
        if not self.still_held(links):
            # 監視が読んだ後に装置のボタンが押された(後ろに何もなく終えるレーンを完了と
            # 書く前に見る)。次の周期の監視が決める
            return False
        by_dev = {link.registered_id: link for link in links}
        if self._restart_cancelled(gid, set(by_dev)):
            # 読み直している間に止め方が押された。後ろに何もなく終えるレーンも完了と
            # 書かない。どのレーンの終わりも、押した止め方で監視が書く(待つ間の終わり)
            return False
        le = g.get("lap_end") or {}
        names = self.c._names()
        plan, finished, was = [], [], {}
        # レーンごとの、予約で停止した時刻。続きを始めるまで待った間は、そのレーンが
        # 進んでいないので、周の時間から除く(終えたレーンは終わりの行の stopped_at、
        # 続けるレーンは続きを始めた後の最初の周の行か終わりの行の held_s。Coupler)
        held_from = {}
        # 続きを始められない理由(最初に見つけたもの)。全レーンを見終えてから決める。
        # 見つけた所で戻ると、並びで後ろのレーンが、後ろに何もなく終えるレーンでも
        # 完了と記録されない
        cannot = None
        for x in _taking_part(g):
            link = by_dev.get(x.get("dev"))
            art = (self.c.project.sent.built(x.get("proc_id") or "", x["dev"])
                   if link is not None else None)
            if art is None:
                cannot = cannot or ("装置へ送った手順のコピーを読み込めません",
                                    "先頭か別の開始位置から開始してください")
                continue
            laps, finals = (le.get("plans") or {}).get(x["dev"], (None, True))
            st = link.status or {}
            lm = st.get("last_mark") or {}
            kind, start, done = self._resume_start(art, st, laps)
            if kind == "start" and (start or {}).get("after"):
                # くり返しの後ろから続ける。後ろに何もなければ、そこで終わりである
                # (周回数が無限の手順は、送ったコピーの対応表では分からない)
                kind = self._rest_build(x, art, lm, laps, finals)[0]
            if st.get("stop_reason") and getattr(link, "run_ended_at", 0.0):
                # 収集が見積もった停止した時刻(監視が終わりを書くときと同じ)。
                # 見積もりがなければ(padcue を起動し直した後など)、除かない
                held_from[x["dev"]] = self.c._end_time(link, time.time())
            if kind == "finished":
                # 終わりの記録は新しい周回数で書く(開始の周回数では、終えた周と
                # 一致しない)
                finished.append((link, {**x, "laps": laps}))
                continue
            if kind == "failed":
                cannot = cannot or ("停止した位置から開始できません",
                                    "別の開始位置を選択してください")
                continue
            w = (le.get("waited") or {}).get(x["dev"])
            if (start is not None and w and w[1] == lm.get("index")
                    and list(w[2]) == list(lm.get("counters") or [])):
                period = (link.info.frame_period_ns if link.info
                          else proto.DEFAULT_FRAME_PERIOD_NS)
                start = {**start, "waited": max(0, round(w[0] * 1e9 / period))}
                # 待っていたのと同じ待ちから続ける。通知の監視は、これを新しい
                # 待ちとみなさない(その待ちを知って画面を操作した人に鳴らし直さない)
                # 始めなければ _restart がクリアする
                with self.c._lock:
                    self._resumed_waits.add(x["dev"])
            item = {"dev": names.get(x["dev"], ""), "name": x.get("proc") or "",
                    "laps": laps, "start": start, "finals": finals}
            if done is not None:
                item["laps_done"] = done
            plan.append(item)
            was[x["dev"]] = x
        if self._restart_cancelled(gid, set(by_dev)):
            # 続ける位置を求めている間に止め方が押された。頭の確認と同じく、完了も
            # 続きを始められない理由も書かない。どのレーンの終わりも、押した止め方で
            # 監視が書く(待つ間の終わり)
            return False
        # 終えたレーンの終わりを先に記録する。続きを始めると、そのレーンも続きの
        # グループへ移す(グループが分かれると、完了の通知が途中で出る)
        for link, x in finished:
            if self.c._mark_lane_ended(gid, x["dev"]):
                self.c._note_run_end(link, x, why=proto.STOP_REASON_FINISHED,
                                     stopped=held_from.get(x["dev"]))
        if cannot is not None:
            # 続きを始められないレーンがある。終えたレーンは完了として記録したので、
            # 理由を付けない(開始を拒否されたときと同じ。仕様 D11)
            self._failed(gid, links, *cannot,
                         skip={x["dev"] for _link, x in finished})
            return False
        if plan and self._restart_cancelled(gid, was):
            self._end_cancelled(gid, was, by_dev)
            return False
        continued = None
        if plan:
            taking = {x.get("dev") for x in _taking_part(g)}
            continued = {
                "gid": gid,
                "started_at": g.get("started_at"), "notes": g.get("notes"),
                "lanes": was, "ended": [x["dev"] for _link, x in finished],
                # 終え方から外れたレーン(_leave)も、続きのグループへ移す。まだ押した
                # 止め方で停止する途中でも移す(グループを分けない)
                "left": [x.get("dev") for x in g.get("lanes") or []
                         if x.get("dev") not in taking],
                "order": [x.get("dev") for x in g.get("lanes") or []],
                "held_from": {d: held_from[d] for d in was if d in held_from},
                # 続きの開始の時刻にする、元の実行の開始の時刻(Coupler._record_group)
                "since": {d: t for d, t in (le.get("since") or {}).items() if d in was}}
            res = self.c.start_group(plan, continuing=continued)
            if res.get("changed"):
                # 転送の間に止め方が押されたか(中止)、待っていたレーンの装置の
                # ボタンが押された。ボタンなら、次の周期の監視がそのレーンを原因に
                # 待つ間を終える
                if self._restart_cancelled(gid, was):
                    self._end_cancelled(gid, was, by_dev)
                return False
            if not res.get("ok"):
                # 拒否の理由が次にできることを示す。終えたレーンは完了として記録した
                # ので、理由を付けない(同じレーンで「完了」と「停止しました」を書かない)
                self._failed(gid, links, res.get("error") or "",
                                     skip={x["dev"] for _link, x in finished})
                return False
            if self._restart_cancelled(gid, was):
                # RUN を送っている間に停止が押された。始めたレーンをすぐ停止させる
                self.c.stop_lanes("immediate", [names.get(d, "") for d in was])
                return False
        with self.c._lock:
            cur = self.c._group_by_id(gid)
            if cur is not None:
                cur.pop("lap_end", None)
                self.c._save_runstate()
        # 始めている間に止め方を押されたレーン(stop_of が lap_end の late に保存し、
        # Coupler._record_group が続きのレーンの記録に置いて、ここへ渡した)
        late = {d: m for d, m in ((continued or {}).get("late") or {}).items()
                if d in was}
        for how in ("immediate", "graceful"):
            devs = [names.get(d, "") for d, m in late.items() if m == how]
            if devs:
                # 続きのグループの記録には、押した止め方を置いてある(Coupler の
                # _record_group)。そのレーンだけを停止させ、ほかのレーンへは広げない
                self.c.stop_lanes(how, devs)
        if finished and not plan:
            self.c._finish_group(gid)
        return bool(plan)

    def _end_cancelled(self, gid: str, was: dict, by_dev: dict) -> None:
        """続きを始めている途中に止め方が押された(_restart_with)。停止したまま終える
        (周回数を変える予約は、止め方を受けた stop_lanes が drop_of でもうやめている)。
        終わりは、続きを始める前に押したときと同じく、今のレーンの記録(押した止め方、
        押した時刻。drop_of)と、装置が停止した時刻で、元の周回数で書く。始めた時点の
        コピーには、押した止め方と時刻がない。

        続きの RUN を送り始めた後に押され、最初の RUN が送れずに中止したときは、
        どの装置も続きを始めていない。drop_of が保持した「周回数を元に戻した行」を、
        終わりの行の前に書く。"""
        with self.c._lock:
            at, rows = self._backs_kept.pop(gid, (0.0, []))
        self._log_backs(rows, at, by_dev)
        for dev in was:
            link = by_dev.get(dev)
            with self.c._lock:
                cur = next((dict(x) for x in (self.c._group_by_id(gid) or {})
                            .get("lanes") or [] if x.get("dev") == dev), None)
            if (link is not None and cur is not None
                    and self.c._mark_lane_ended(gid, dev)):
                self.c._note_run_end(link, cur,
                                     at=self.c._end_time(link, time.time()))

    @staticmethod
    def _resume_start(art, st: dict, laps) -> tuple:
        """予約で停止した状態 st(送った手順のコピー art の実行)から、新しい周回数 laps
        (無限は None)で続きを始める位置。

        返すのは (種類, 開始位置, 周回数のくり返しの後ろから続けるときに終えた周の
        数) で、種類は "start"(開始位置から続ける。先頭なら開始位置は None)、"finished"
        (くり返しを抜けて、後ろに何もない。そこで終わり)、"failed"(停止した位置から
        始められない)のどれかである。_restart_with と、続きの長さを数える ends_at が
        同じ決め方を使う。
        """
        lm = st.get("last_mark") or {}
        # 周の境の区切り。周の先頭や抜けた所にラベルがあっても、こちらを見る
        lap_mark = positions.lap_mark_at(art.marks, lm.get("index"))
        out = Project.lap_exit(art.lap_map, lm)
        if (out and lap_mark and art.laps != INFINITE
                and (laps is None or laps > art.laps)):
            # くり返しを抜けた所で停止したが、新しい周回数はまだ先の周まである
            return ("start", positions.start_from_mark(lap_mark, art.laps + 1, None),
                    None)
        pos = positions.stopped_at(art, st)
        if pos is None:
            return "finished", None, None
        start = pos.get("start")
        if start is None and not pos.get("head"):
            return "failed", None, None
        # 周の境で停止したときは、同じレコードに元の周回数でその周に選ばれた選択肢の中の
        # ラベルがあっても、停止した位置は周の先頭である(positions.stop_mark)。
        # 続きはその周の選択肢を新しい周回数で選び直す(最初からその周回数で始めたのと
        # 同じ動き。runbook 2-4)
        if out:
            return "start", start, art.laps
        if (lap_mark and start and start.get("turns")
                and laps is not None and start["turns"][0] > laps):
            # 新しい周回数を超える周の先頭。周回数のくり返しの直後から始める。後ろに
            # 何もなければ、そこで終わりである。手順全体を囲むくり返し(周の境の区切りが
            # ブロックを持たない)の後ろには何もない。周回数が無限の変換にはくり返しを
            # 抜けた所がないので、対応表では分からない
            if (not lap_mark.get("block")
                    or any(item.get("exit") == "last" for item in art.lap_map)):
                return "finished", None, None
            return "start", {"path": list(lap_mark.get("path") or []),
                             "block": lap_mark.get("block", ""), "turns": [],
                             "after": True}, start["turns"][0] - 1
        return "start", start, None

    def ends_at(self, stops: dict, groups: list | None = None) -> dict:
        """周回の終え方を選んでから続きを始めるまでの、レーンごとの終わる時刻(UNIX 秒。
        画面の「終了予定」)。{装置 ID: 時刻} を返す。

        stops は {装置 ID: (停止する区切り(STATUS の last_mark の形), そこに着く時刻,
        送った手順のコピー, 1 フレームの秒数)} で、画面の状態を作るときに装置の状態から
        求めたもの(先に止まって待つレーンは、止まった区切りと時刻)である。グループの
        全レーンが止まってから続きを始めるので、続きは、止まる所に着く時刻のうち最も遅い
        ものから始まる。終わる時刻は、それにそのレーンの続きの長さを足したものである。
        続きの長さは、停止する区切りから新しい周回数で始める続き(_resume_start)を
        変換して数える。後ろに何もないレーンは、全レーンが揃った時刻に終わる。グループの
        レーンのどれかで求められなければ(待ちを含む、変換できない、続きが終わらない、
        送った手順のコピーが読めない形)、そのグループには出さない。groups は
        Coupler.groups_copy で写したグループの記録で、渡さなければここで写す。
        """
        if groups is None:
            groups = self.c.groups_copy()
        out = {}
        for g in groups:
            le = g.get("lap_end")
            lanes = _taking_part(g)
            if not le or not lanes or any(x.get("dev") not in stops for x in lanes):
                continue
            begin = max(stops[x["dev"]][1] for x in lanes)
            got = {}
            try:
                for x in lanes:
                    lm, _at, art, period = stops[x["dev"]]
                    laps, finals = (le.get("plans") or {}).get(x["dev"], (None, True))
                    n = self._rest_frames(x, art, lm, laps, finals)
                    if n is None:
                        break
                    got[x["dev"]] = begin + n * period
                else:
                    out.update(got)
            except positions.BAD_COPY:
                positions.tell_bad_copy(("ends", g.get("id", "")), _run_of(g))
        return out

    # 続きの変換のキャッシュの数の上限(_rest_build)
    _REST_MEMO_MAX = 64

    def build(self, art, laps, start, finals: bool = True, name: str = "",
              drawn_laps=None) -> tuple:
        """周回の終え方の続きを変換する。Project.build_safe と同じく (変換の結果,
        失敗の文) を返す。

        続きの変換はすべてここを通す。実際の続き(Coupler の _build_lane。図も)、終了
        予定(_rest_build)、メッセージ(Coupler の notes_at_laps。元の開始位置から
        変換する)である。続きは同じ 1 回の実行なので、元の実行(送った手順のコピー
        art)と同じく、開始した時点の定義(保存物)の同じ手順を、同じレーンの数で
        変換する。レーンの数は合流点を待ちにするかを決める(2 本以上で待つ)。グループの
        ほかのレーンが続きを始めずに終えても、レーンの数は変えない。変えると、続きの
        合流点が元の実行と一致せず(待たずに通り、図から待ちが消える)、終了予定を求めた
        続きと実際の続きが別のものになり、合流点で待っていたレーンの続きは、その
        合流点から始められない。
        laps と start と drawn_laps は Project.build_safe と同じ(周回数、変換器の開始
        位置)、name は art が手順の名前を持たないときの名前である。art は保存物
        (sources)を持つこと。
        """
        frozen = self.c.project.sent.frozen(art.sources)
        return frozen.build_safe(art.source_name or name, laps, start, art.lanes,
                                 finals=finals, write=False, drawn_laps=drawn_laps)

    def _rest_frames(self, x: dict, art, lm: dict, laps, finals: bool) -> int | None:
        """グループのレーンの記録 x の実行(送った手順のコピー art)が区切り lm で停止した
        とき、新しい周回数 laps(無限は None)で始める続きの長さ(フレーム数)。後ろに
        何もなければ 0。求められなければ None(待ちを含む、変換できない、続きが
        終わらない)。
        """
        kind, r = self._rest_build(x, art, lm, laps, finals)
        if kind == "finished":
            return 0
        if r is None or r.awaits or r.endless:
            return None
        return r.total_frames

    def _rest_build(self, x: dict, art, lm: dict, laps, finals: bool) -> tuple:
        """グループのレーンの記録 x の実行(送った手順のコピー art)が区切り lm で停止した
        とき、新しい周回数 laps(無限は None)で始める続き。(種類, 変換の結果) を返す。
        種類は _resume_start の種類(くり返しの後ろから続けて、後ろに何もなければ
        "finished")で、変換の結果は "start" のときだけある(変換できなければ None)。

        続きを始めるときと同じ所から始め(_resume_start)、同じく変換する(build)。変換は
        重いので、同じ停止する区切りと周回数の答えをキャッシュする(画面の状態は毎秒
        作られる)。
        """
        key = (art.hash, art.sources, lm.get("index"), tuple(lm.get("counters") or ()),
               laps, finals)
        memo = self._rest_memo
        if key in memo:
            return memo[key]
        st = {"running": False, "awaiting": False,
              "stop_reason": proto.STOP_REASON_AT_MARK, "last_mark": lm}
        kind, start, _done = self._resume_start(art, st, laps)
        r = None
        if kind == "start" and art.sources:
            from .coupler import _start_of
            r = self.build(art, INFINITE if laps is None else laps,
                           _start_of({"start": start}), finals,
                           name=x.get("proc") or "")[0]
            if (r is not None and (start or {}).get("after") and not r.awaits
                    and not r.endless and not r.total_frames):
                # くり返しの後ろから続けるが、後ろに何もない(周回数が無限の手順は、
                # _resume_start が対応表で区別できない)。そこで終わりである。空の
                # 続きを始めると、0 フレームの別の実行として記録が分かれる
                kind, r = "finished", None
        if len(memo) >= self._REST_MEMO_MAX:
            memo.clear()
        memo[key] = (kind, r)
        return kind, r

    def _failed(self, gid: str, links: list, why: str,
                        then: str = "", skip=()) -> None:
        """続きを始められなかった。予約をやめ、理由をレーンに出して記録に残す。

        レーンは止まったままである。開始時のメッセージは動いている間しか出ないので、
        理由はグループのレーンの記録(runstate.json の lap_end_error)に書き、次に
        そのレーンを開始するまで残す(coupling.md D11。padcue を起動し直しても
        消えない)。skip は理由を付けないレーン(装置 ID。先に終えて完了として
        記録したレーン)。通知の監視は、理由のあるレーンを含むグループを完了として
        知らせない(notify.py)。
        停止した所の終わりは、この後に監視が記録する。その実行の止まり方として、続きを
        始められなかったことを、レーンの記録の待つ間の終わり(wait_end の failed。
        drop_of)に置く(Coupler の _note_run_end)。続きの RUN を受けた装置(一部の
        装置が RUN を拒否した)の終わりは、開始が拒否されたときに書いてある(Coupler の
        _end_rolled_back。装置は後始末で続きの手順を動かすので、監視には停止した所が
        読めない)。理由と違い、次の開始の準備ではクリアしない。停止したのを確かめる
        前に次の開始が来ると、前の実行の終わりはその開始が書く(_record_group)。
        """
        msg = sentences("周回の終え方を変更できず、停止しました", why, then)
        at = time.time()
        with self.c._lock:
            cur = self.c._group_by_id(gid)
            if cur is None:
                return
            # 終え方から外れたレーン(_leave)は、もう押した止め方で終わっている
            taking = _taking_part(cur)
            cur.pop("lap_end", None)
            devs = set()
            for x in taking:
                if x.get("dev") not in skip:
                    x["lap_end_error"] = msg
                    if not x.get("ended"):
                        # 止まって待っていたレーンの実行が終わったのは、続きを拒否された
                        # この時刻である(drop_of の at と同じ。Coupler の
                        # _note_run_end)。続きを待つ間は 1 回の実行の途中なので、周の
                        # 境で停止した時刻ではない。周回数は変えられなかったので元のまま
                        x["wait_end"] = {"at": at, "failed": True}
                    devs.add(x.get("dev"))
            self.c._save_runstate()
        for link in links:
            if link.registered_id in devs:
                # 理由の行は、終わりの行(この時刻)の後に並べる(停止した、その理由の順)
                self.c._log(link, "PC_LAP_END_FAILED", why=msg, at=at + 0.001)

    def live(self, gid: str) -> dict | None:
        """そのグループの周回数を変える予約(lap_end)の今のコピー。なければ None。"""
        with self.c._lock:
            le = (self.c._group_by_id(gid) or {}).get("lap_end")
            return json.loads(json.dumps(le)) if le else None
