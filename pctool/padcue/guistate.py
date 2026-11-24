"""操作画面のサーバーが画面へ渡す状態(/api/state)を組み立てる。

画面は 1 秒ごとにこれを取得し直し、どの画面もここから描く。gui.py の _Handler が
StateMixin を受け継いで使う。
"""
from __future__ import annotations

import functools
import hashlib
import time
from pathlib import Path

from . import binfmt, lapend, positions, proto, registry
from .client import failure_of, firmware_problem, firmware_too_new
from .conditions import ConditionStore, validate_video
from .condwatch import arm_kinds, human_only
from .names import name_key
from .sources import FileSource


@functools.cache
def _web_rev() -> str:
    """画面のファイルのリビジョン(起動した時点の web/ の中身から 1 回だけ作る)。"""
    h = hashlib.blake2b(digest_size=8)
    root = Path(__file__).parent / "web"
    for f in sorted(root.iterdir()):
        if f.is_file():
            h.update(f.name.encode("utf-8"))
            h.update(f.read_bytes())
    return h.hexdigest()


# 実行の間に Switch との接続が変わって停止したときの記録(firmware の main.c の
# stop_run_for_usb。USB の切断とスリープのほか、本体が装置を認識し直したときも停止する)
_SWITCH_FAULTS = frozenset({"USB_UMOUNT", "USB_SUSPEND", "USB_MOUNT"})
# 異常停止の原因を探す記録の行数(画面のログの 1 頁と同じ)
_FAULT_LOOKBACK = 2000


def fault_causes(entries: list[dict]) -> dict[str, str]:
    """装置ごとの、いまの異常停止の原因になった Switch との接続の記録の種類
    (装置 ID → 種類)。

    entries は記録を書いた順に並べたものである(同じ装置の行は、装置が書いた順)。
    装置は、その出来事、停止(RUN_ABORT)、異常停止に移った状態の行の順に書く。そこで、
    その装置の最後の異常停止に移った状態の行の直前の行(停止の行は飛ばす)が接続の
    記録なら、それを原因とする。手順データの異常(ENGINE_FAULT)などの異常停止は
    返さない(装置の記録は理由を持たない)。異常停止から抜けた行が後にある装置も
    返さない(いまの異常停止の行を、まだ受け取っていない)。
    """
    return {dev: kind for dev, kind in _fault_verdicts(entries).items() if kind}


def _fault_verdicts(entries: list[dict]) -> dict[str, str]:
    """fault_causes の中身。記録の中に異常停止に入った行か抜けた行がある装置の、
    原因の種類。原因の分からない異常停止と、抜けた行が後にある装置は空で返す
    (記録から「原因はない」と分かった装置である。記録にない装置は返さない)。"""
    out: dict[str, str] = {}
    last: dict[str, str] = {}
    for e in entries:
        kind = str(e.get("kind") or "")
        dev = str(e.get("dev") or "")
        if not dev or kind.startswith("PC_"):
            continue            # PC が書いた行は、装置の記録の並びに入らない
        if kind == "STATE" and e.get("b") == proto.STATE_ERROR:
            out[dev] = last.get(dev, "") if last.get(dev) in _SWITCH_FAULTS else ""
        elif kind == "STATE" and e.get("a") == proto.STATE_ERROR:
            out[dev] = ""
        if kind != "RUN_ABORT":
            last[dev] = kind
    return out


# 異常停止の原因の、記録のファイルごとのキャッシュ。状態は毎秒取られるが、異常停止の間は
# 装置が動かないので、記録はほとんど増えない。書き込み中のファイルの大きさと更新
# 時刻が前と同じなら、読み直さずに前の答え(_fault_verdicts)を使う
_fault_memo: dict[Path, tuple[tuple[int, int], dict[str, str]]] = {}


def _fault_verdicts_of(logs) -> dict[str, str]:
    """記録(RunLog)の新しい方から、装置ごとの異常停止の原因を調べる
    (_fault_verdicts)。"""
    path = logs.path()
    try:
        st = path.stat()
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        return _fault_verdicts(logs.read(_FAULT_LOOKBACK))
    hit = _fault_memo.get(path)
    if hit is not None and hit[0] == sig:
        return hit[1]
    got = _fault_verdicts(logs.read(_FAULT_LOOKBACK))
    _fault_memo[path] = (sig, got)
    return got


def _fault_causes_of(logs) -> dict[str, str]:
    """記録(RunLog)から、装置ごとの異常停止の原因を調べる(fault_causes)。"""
    return {dev: kind for dev, kind in _fault_verdicts_of(logs).items() if kind}


# 異常停止の間に分かった原因(記録のファイル -> 装置 ID -> 種類)。原因は異常停止に
# なった時点で決まり、その後は変わらない。記録から調べるのは新しい方の 2000 行だけ
# なので、ほかのレーンが動き続けて行が増えるか、ログを削除すると、その行は読めなく
# なる。読めない間は覚えた原因を出し、異常停止から抜けたのを読んだら忘れる
# (_fault_of、_forget_fault)
_fault_kept: dict[Path, dict[str, str]] = {}


def _fault_of(logs, dev: str, verdicts: dict[str, str]) -> str:
    """異常停止の装置 dev の原因(なければ空)。記録の新しい方に、その装置が異常停止に
    入ったか抜けた行があれば、それで決めて覚える。なければ覚えた原因を使う。"""
    kept = _fault_kept.setdefault(logs.path(), {})
    if dev in verdicts:
        kept[dev] = verdicts[dev]
    return kept.get(dev, "")


def _forget_fault(logs, dev: str) -> None:
    """装置 dev が異常停止でないと分かった。覚えた原因を忘れる(_fault_of)。"""
    (_fault_kept.get(logs.path()) or {}).pop(dev, None)


def _carried_stop(project, lane: dict) -> dict | None:
    """装置の状態が持たない実行のレーンの記録(前の装置から引き継いだ記録
    Coupler.carried_lane と、起動し直した装置の最後の実行の記録 Coupler.ended_lane)の、
    停止した位置。

    装置の状態から出すとき(positions.stopped_at)と同じ形にする。止まり方と最後に
    通った区切りは、その実行の終わりの記録(result の why と mark)が持つ。終わりの
    記録は、最後の周を実行し終えた所で停止した実行と、周回数を変えて周の境で終えた
    実行を完了と書くので、そこには停止した位置を出さない(装置の状態から出すときも
    出さない)。
    """
    r = lane.get("result") or {}
    mark = r.get("mark") or [None, []]
    art = project.sent.built(lane.get("proc_id") or "", lane.get("sent_dev") or "")
    if art is None or len(mark) != 2:
        return None
    st = {"running": False, "awaiting": False, "stop_reason": r.get("why") or "",
          "last_mark": {"index": mark[0], "counters": list(mark[1] or [])}}
    return positions.still_startable(project, positions.stopped_at(art, st))


def _tell_bad_copy(link, running_id: str) -> None:
    """そのレーンの送った手順のコピーが読めない形(positions.BAD_COPY)だった理由を、
    その実行(実行中の手順の id と、実行を始めた時刻)について 1 回だけ、画面の
    サーバーの出力に出す(except の中から呼ぶ。positions.tell_bad_copy)。"""
    positions.tell_bad_copy(link.registered_id, (running_id, link.run_started_at))


# 線と止まる先を求める所が書く欄。求められなかったレーンからは除く
_POSITION_KEYS = ("stop_target", "stop_ends_at", "at_frame", "at_since", "at_hold",
                  "at_open", "lap_span")


def _stop_ends_at(link, d: dict, frames) -> float | None:
    """装置が、最後に通った区切りから frames フレーム進んで停止する時刻(UNIX 秒。画面の
    「終了予定」)。

    起点は、装置がその区切りを通った時刻(収集が装置の時計から出したもの。
    DeviceLink.passed_at)で、読むたびにばらつかない。区切りをまだ通っていなければ、
    装置の時計から出した開始の時刻(DeviceLink.frame0_at。線の起点と同じ)である。
    frames が分からないとき(待ちを越える)と、起点の時刻が分からないとき(収集がまだその
    区切りを覚えていない)は None を返す。起点を 0 とみなすと 1970 年になる。
    """
    if not isinstance(frames, int):
        return None
    period = (link.info.frame_period_ns if link.info
              else proto.DEFAULT_FRAME_PERIOD_NS) / 1e9
    since = (link.passed_at(d["last_mark"]) if d.get("last_mark")
             else link.frame0_at)
    return since + frames * period if since else None


def _run_ends_at(link, st: dict) -> float | None:
    """実行が最後まで進んで終わる時刻(UNIX 秒。停止の予約のない実行の、画面の
    「終了予定」)。st は装置の状態である。

    装置の時計から出した開始の時刻(DeviceLink.frame0_at。区切りを通った時刻と同じく、
    読むたびにばらつかない)に、総フレーム数を足す。今の時刻に最後に読んだ経過フレーム数
    からの残りを足すと、読んでから画面に送られるまでの時間だけ遅れ、読むたびにばらつく。
    動いていないとき、総フレーム数が 0(終わりのない手順)のとき、開始の時刻がまだ
    分からないときは None を返す。
    """
    total = st.get("total_frames")
    if not (st.get("running") or st.get("awaiting")) or not isinstance(total, int) \
            or total <= 0 or not link.frame0_at:
        return None
    period = (link.info.frame_period_ns if link.info
              else proto.DEFAULT_FRAME_PERIOD_NS) / 1e9
    return link.frame0_at + total * period


def _last_proc_in(groups: list, dev_id: str) -> str:
    """グループの記録の写し groups で、そのレーンで最後に開始した手順の id
    (Coupler.last_proc_of と同じ決め方を、写しから)。なければ空。"""
    if not dev_id:
        return ""
    for g in reversed(groups):
        for x in g.get("lanes") or []:
            if x.get("dev") == dev_id:
                return str(x.get("proc_id") or "")
    return ""


def _last_run_in(groups: list, dev_id: str) -> list | None:
    """グループの記録の写し groups で、そのレーンで最後に開始した実行の、装置が返す
    実行の識別(レーンの記録の run)。なければ None。"""
    if not dev_id:
        return None
    for g in reversed(groups):
        for x in g.get("lanes") or []:
            if x.get("dev") == dev_id:
                return x.get("run")
    return None


class StateMixin:
    """画面へ渡す状態を組み立てる(gui.py の _Handler が受け継ぐ)。"""

    def _lane_video_conditions(self) -> set[str]:
        """判定する映像が「装置に設定している映像」の条件の、名前の同一性のキー
        (name_key)。選択肢は綴りの違う名前で条件を指しうる(file-format.md §2)。
        """
        store = ConditionStore(self.project.root)
        out = set()
        for name in store.names():
            try:
                # 映像の定義だけを読む。load は基準画像を復号して縮めるので、
                # 毎秒の状態取得のたびに呼ぶと、条件の数に比例して時間がかかる
                if (store.source_of(name) or {}).get("kind") == "lane":
                    out.add(name_key(name))
            except (OSError, ValueError):
                continue
        return out

    def _state(self) -> dict:
        # 手順・フレーム表・条件の名前を読み始めた時刻(名前の変更の履歴を、この後に
        # 変えた名前で捨てない。_renames_now)
        listed = time.monotonic()
        org = self.project.load_proc_org()
        hidden_set = set(org["hidden"])
        lane_conds = self._lane_video_conditions()
        uses = self._running_uses()
        # 条件の名前は並べ替えた順で、毎秒 1 回だけ数える
        cond_names = ConditionStore(self.project.root).names()
        have = set(cond_names)
        procs = []
        # 使用している場所の数(一覧の行のメニューの「使用している場所を表示」に出す)
        used, join_names = self.project.usage_and_joins()
        for name in self.project.procedure_names():
            ps = self.project.proc_status(name, have)
            r, detail = ps["result"], ps["error_at"]
            # rev は図と一覧のリビジョン、file_rev は手順のファイルそのものの
            # リビジョン、in_use はこの手順を使って実行しているレーン(削除できない
            # 理由)、used は使用している場所の数
            base = {"name": name, "hidden": name in hidden_set,
                    "rev": ps["rev"], "file_rev": ps["file_rev"],
                    "in_use": uses.get(("procedure", name_key(name)), []),
                    "used": used.get(("call", name_key(name)), 0)}
            if r is None:
                # error は全文(ファイルの場所つき)、error_msg は文だけ。一覧の
                # 「エラー」の説明には文だけを出す
                procs.append({**base, "error": detail["full"],
                              "error_msg": detail["msg"],
                              # 失敗が呼び出し先の中なら、文が開いて修正するよう求める
                              # その手順(レーンの操作がそれを開く)
                              **({"error_callee": detail["callee"]}
                                 if detail.get("callee") else {})})
            else:
                procs.append({
                    **base,
                    "frames": r.total_frames,
                    "seconds": round(r.seconds, 1), "hash": r.hash,
                    # 待ち(選択待ち・合流点・スクリプト)があるか。待つ時間と、
                    # 選ばれるまで分からない選択肢の中身は長さに入らないので、
                    # 画面は長さを下限として「90F〜」と出す
                    "has_waits": bool(r.awaits),
                    # 終わりのない手順か(無限のくり返しがある)。長さの 0 は終わりの
                    # ない手順でも、選択待ちの選択肢の中身だけの手順でも起きるので、
                    # 画面はこれで長さを「無限」と出す
                    "endless": r.endless,
                    "warnings": len(ps["warnings"]), "precondition": r.precondition,
                    # 装置に設定している映像を見る条件を選択条件に持つか。画面は、
                    # これが真の手順を選んだレーンにだけ「映像 未設定」のバッジを出す
                    "needs_lane_video": any(
                        name_key(c) in lane_conds
                        for awt in (r.awaits or [])
                        for c in (awt.get("conds") or []) if c),
                    # 装置に入るレコード数と、その上限。手順の
                    # 状態として常に返す。編集画面が余裕を出す
                    "records": r.events, "records_max": binfmt.RECORD_CAPACITY,
                })
        cfg = self.project.load_config()
        # フレーム表と条件の一覧とリビジョン。編集画面は、開いている物の
        # リビジョンがほかで変わったことをこれで知る。読めない物には、その理由(error)を
        # 付ける。一覧は手順と同じく、行に「エラー」と出す
        frametables, conds = [], []
        for kind, names, out_list in (
                ("frametable", self.project.frametable_names(), frametables),
                ("condition", cond_names, conds)):
            revs = {n: self.project.file_rev(kind, n) for n in names}
            bad = self.project.list_problems(kind, revs)
            for n, rev in revs.items():
                item = {"name": n, "rev": rev,
                        "in_use": uses.get((kind, name_key(n)), []),
                        "used": used.get((kind, name_key(n)), 0)}
                if n in bad:
                    item["error"] = bad[n]
                out_list.append(item)
        out = {"procedures": procs, "frametables": frametables, "conditions": conds,
               # ほかで名前が変わった物(開いたままの画面が付いていく)
               "renames": self._renames_now({
                   "procedure": {p["name"] for p in procs},
                   "frametable": {p["name"] for p in frametables},
                   "condition": {c["name"] for c in conds}}, listed),
               # 画面のファイル(web/)のリビジョン。開いたままの画面は、読み込んだときの
               # リビジョンと比べて、サーバーを新しくした後の古い画面であることを知る
               "web_rev": _web_rev(),
               # デモの起動か(gui --mock)。画面は実機を登録する操作を出さない
               "demo": bool(self.demo),
               # 並び順のファイルが読めない理由(読めれば空)
               "order_error": self.project.order_error(),
               "project": str(self.project.root),
               "proc_folders": org["folders"],
               # 合流点に付けた名前(編集の画面が、合流点の名前の欄の候補に出す)
               "join_names": join_names,
               # Windows の通知を出す場面(この PC の設定)。音とタブへの
               # 表示は見る人の設定なのでブラウザーに残る
               "os_notify": self.project.os_notify(),
               "consoles": cfg.get("consoles") or {}}
        # 装置は手順を名前ではなく中身から作った id で報告する。その id を名前へ
        # 戻す表である。正式な出所は装置へ送った手順のコピー(sent/)で、これはコピー
        # から見つけられないときの推定である(周回数 1 で変換した id しか
        # 含まれない)
        id2name = {p["hash"]: p["name"] for p in procs if p.get("hash")}
        # 装置プールの収集キャッシュの値をすぐに返す(装置への I/O はしない)。
        # 片方が無応答でも、その装置の error になるだけでほかの装置は即座に返る
        links = self._pool().links()
        self._wait_first_collect(links)
        join_waits = self._coupler().join_waits()
        # グループの記録と装置の状態を、同じ時点で写す(Coupler.view)。1 回の取得の
        # 中では、どの読み取りもこの写しを使う
        groups, readings, starting = self._coupler().view(links)
        # 装置 ID ごとの写し(グループの記録の読み取りに渡す)
        read_by_id = {link.registered_id: readings[id(link)] for link in links
                      if link.registered_id}
        holds, lap_end_errors = self._coupler().lap_ends.holds_and_errors(
            groups, read_by_id)
        devices = []
        # 実行中のレーンの収集と送った手順のコピー(レーンの名前 -> (link, art))。完了に
        # 連動して周を待つレーンの終了予定を、グループの記録を読んだ後で求めるのに使う
        running_lanes = {}
        # 異常停止の原因(_fault_verdicts。要るときに 1 回だけ読む)
        faults = None
        for link in links:
            # このレーンのどの値も、上で写した状態とその時刻から出す(LinkReading)。
            # 状態と時刻を別々の時点で読むと、終了予定や線の起点が求められなくなる
            link = readings[id(link)]
            status, run0 = link.status, link.run_started_at
            # id は記録とグループが装置を指す識別子でもある。割り当てを解除した
            # 装置では空になる
            d = {"name": link.cfg.get("name", ""), "id": link.cfg.get("id", ""),
                 "host": link.cfg.get("host", ""),
                 "port": int(link.cfg.get("port", proto.DEFAULT_PORT)), "at": link.at,
                 # 最後に状態を読めた時刻。接続が途切れたレーンに「最終確認」として出す
                 "status_at": link.status_at}
            if link.console_id:
                # つながっている本体(Switch)の識別子。名前は padcue.json の
                # consoles で付け替えられる
                d["console_id"] = link.console_id
            former = registry.former_ids(link.cfg)
            if former:
                # この行に前に割り当てていた装置。ログは、その装置の過去の行も
                # この行の名前で出し、絞り込みにも含める(registry.forget_device)
                d["former_ids"] = former
            if link.save_error:
                # 装置から読んだ記録などを PC に保存できない。装置との通信は
                # 正常なので、つながらない理由(error)とは別に出す
                d["save_error"] = link.save_error
            if link.error:
                d["error"] = link.reason
                # 失敗の種類(コマンドの終了コードを、直結と同じに決めるのに使う)
                d["failure"] = (failure_of(link.error_exc)
                                if link.error_exc else "unreachable")
                # この行が覚えている装置と、そこにいる装置が一致しない。
                # 装置を交換したときの正規の対処(割り当ての解除)は画面から
                # しか押せないので、押せる場面かどうかを画面に伝える
                d["mismatch"] = link.mismatch
            # 最後に読めた値は、応答がなくなった後も含め続ける(「未検出」の
            # 間は最後に読めた値を出す)。読む側は error と gone を見て、
            # いつの値かを判断する
            if link.info is not None:
                d.update({
                    "fw": link.info.fw_version,
                    "binterval": link.info.binterval,
                    "partition": link.info.partition,
                    "rolled_back": link.info.rolled_back,
                    "crash": link.info.crash,
                    "reset_reason": link.info.reset_reason,
                    "frame_period_ns": link.info.frame_period_ns,
                    **status,
                })
                if link.binterval_saved not in (None, link.info.binterval):
                    d["binterval_saved"] = link.binterval_saved
                why = firmware_problem(link.info.fw_version)
                if why:
                    # この padcue で使えないファームウェア。装置の行で更新を求め、
                    # 開始は coupler が同じ文で拒否する
                    d["fw_problem"] = why
                if firmware_too_new(link.info.fw_version):
                    # この padcue より新しい。求める対処は padcue の更新なので、
                    # 装置の行はファームウェアの更新のボタンを出さない(文では
                    # 区別しない)
                    d["fw_too_new"] = True
                hold = link.registered_id in holds
                job = self._ota_jobs.get(link.registered_id)
                if job is not None:
                    d["ota"] = {**job, "rebooted": link.connects != job["connects"]}
                if hold:
                    # 周回を終えるために予約で止まり、ほかのレーンが停止するのを
                    # 待っている。続きを始めるまでも 1 回の実行の途中なので、
                    # 実行中として出す(停止した位置を出さず、開始を押させない。
                    # 待ちで止まったときは、下でその待ちとして出す)
                    # (サーバーを起動し直した後は、最後に動かした手順を runstate.json
                    # から取り出す)
                    d.update({"running": True, "state": "RUNNING",
                              "proc": link.last_proc or _last_proc_in(
                                  groups, link.registered_id),
                              "lap_end_hold": True})
                if d.get("state") == "ERROR":
                    # 異常停止の原因が Switch との接続なら、その装置の記録の
                    # 種類(装置の行が原因を示す)。記録を読むのは、異常停止の装置が
                    # あるときだけで、記録が変わっていなければ前の答えを使う。
                    # 異常停止の間は、記録から読めなくなっても同じ原因を出す
                    if faults is None:
                        faults = _fault_verdicts_of(self.project.logs)
                    cause = _fault_of(self.project.logs, link.registered_id, faults)
                    if cause:
                        d["fault"] = cause
                else:
                    _forget_fault(self.project.logs, link.registered_id)
                # 装置は手順の名前を持たない。状態の "proc" は手順のバッファーにある
                # 手順の id で、実行中かどうかは表さない(実行が終わっても残り、
                # 停止した後に別の手順を送るとその id になる)。実行中のときだけ
                # 実行中の手順として扱い、画面に出すために名前へ戻す。id そのものは
                # running_id として残し、現在位置を出すのに使う
                running_id = (d.get("proc") or ""
                              if d.get("running") or d.get("awaiting") else "")
                d["running_id"] = running_id
                # 描画用の変換結果は装置ごとに残してある(図と位置の対応付けのキー)
                lane_dev = link.registered_id
                # 停止した後に「停止した位置」を出すには、最後に動かした id が
                # 必要である。収集が実行中に見た id を覚えている。覚えている値は
                # サーバーを起動し直すと消えるので、そのときは runstate.json から
                # 取り出す
                art_id = (running_id or link.last_proc
                          or _last_proc_in(groups, link.cfg.get("id") or ""))
                # 何周目かは、装置が返す「最後に通った区切り」と、変換のときに
                # 作った対応表から出す。装置は周回を数えない。
                # 周回数は手順の中のくり返しの回数として入るので、周回数が
                # 違えば別の id になる。手順の一覧は周回数 1 の id しか持たない
                # ので、名前も変換したときの送った手順のコピーから取り出す
                art = self.project.sent.built(art_id, link.registered_id)
                lm = d.get("last_mark") or {}
                if hold and art is not None and any(
                        a.get("index") == lm.get("index") for a in art.awaits or []):
                    # 周回を終えるために待ち(選択待ち・合流点)で止まり、ほかの
                    # レーンが停止するのを待っている。続きもその待ちから始まるので、
                    # その待ちとして出す(待ちの種類のバッジと待ちの行)。待ちの行の
                    # ボタンは、続きが始まるまで画面が押せなくする(lap_end_hold)。
                    # running は真のままにする(装置は、待ちの間も running を真で返す。
                    # 偽にすると、画面は「区切りで停止」を押せなくし、一括操作の行の
                    # ▾ に理由を出さない。周の境で止まって待つレーンでは押せる)
                    d.update({"awaiting": True, "state": "AWAITING",
                              "event_index": lm["index"]})
                d["proc"] = ("" if not running_id else
                             (art.name if art is not None
                              else id2name.get(running_id, "")))
                if art is not None and running_id and not art.lap_map:
                    # 周回数のくり返しより後ろから始めた実行。周がないので、
                    # 何周目かも周回数も出さない(周回を終えるために後ろから
                    # 続けた実行は、下の lane_done が元の周で上書きする)
                    d["nolaps"] = True
                elif art is not None and running_id:
                    # 周回数のくり返しを抜けた後(後処理の中のラベルを通った後)も、
                    # 抜けた所と同じ周(最後の周)を出す
                    try:
                        lap = positions.lane_lap(art, d.get("last_mark"))
                        if hold:
                            # 周回を終えるために周の境で止まって待つ間は、終えた周を
                            # 出す。止まったのは次の周の先頭の区切りだが、その周はまだ
                            # 始まっていない(線も図の周の終わりに置く。下の
                            # lap_end_frame)
                            lap = positions.ended_lap(art, d) or lap
                    except positions.BAD_COPY:
                        # 送った手順のコピーが読めない形。周を出さず、ほかの欄とほかの
                        # レーンは返す(下の _put_position の受けと同じ)
                        _tell_bad_copy(link, running_id)
                        lap = None
                    if lap is not None:
                        d["lap"] = lap
                    d["laps_total"] = art.laps
                if art is not None:
                    # 停止した位置。今の手順でもそこから始められるときだけ出す(停止した
                    # 後に手順を修正して、その位置がなくなることがある)
                    try:
                        d["stopped_at"] = positions.still_startable(
                            self.project, positions.stopped_at(art, d))
                    except positions.BAD_COPY:
                        _tell_bad_copy(link, running_id)
                        d["stopped_at"] = None
                fig = None
                if art is not None and running_id:
                    running_lanes[d["name"]] = (link, art)
                    # 実行中のレーンが描く図(今の周と、その周に選んだ選択肢で描く)と、
                    # そのリビジョン。画面はリビジョンもキーにして図を取得し直すので、
                    # 選択肢を選んだときと、周が替わって図が変わるときに、描き直す。
                    # この後の線と止まる先も、同じ図の上で求める
                    try:
                        fig = self._figure(d, link, art, lane_dev, hold, run0,
                                           groups)
                    except positions.BAD_COPY:
                        _tell_bad_copy(link, running_id)
                        d.pop("next_lap", None)
                    d["drawn_rev"] = fig.rev if fig is not None else ""
                    # 実行を始めた後に定義(手順、呼び出し先、フレーム表、条件)か
                    # 装置の映像が変わったか。実行は始めたときの中身で動き続け、図も
                    # その中身で描く。変更が次の開始から使われることを、画面が知らせる
                    d["edited_since_start"] = self.project.sent.edited_since(
                        art, link.cfg.get("video"))
                if art is not None:
                    # 「区切りで停止」の止まる先と、図の上の現在位置(線)
                    try:
                        self._put_position(d, link, art, lane_dev, hold, fig)
                    except positions.BAD_COPY:
                        # 送った手順のコピーが読めない形(項目の数が違うなど)。この
                        # レーンには線と止まる先を出さず、ほかの欄とほかのレーンは
                        # 返す。状態の取得が失敗すると、画面全体が使えなくなり、この
                        # レーンを停止する操作もできない。理由は操作画面のサーバーの
                        # 出力に残す(同じ実行については 1 回だけ)
                        _tell_bad_copy(link, running_id)
                        for key in _POSITION_KEYS:
                            d.pop(key, None)
                # 実行中なら、その実行を始めた時刻(画面の「開始」)
                if link.run_started_at:
                    d["run_started_at"] = link.run_started_at
                elif hold and holds[link.registered_id]:
                    d["run_started_at"] = holds[link.registered_id]
                if art is not None and art.awaits and running_id:
                    # 待ちを含む手順は終わりの時刻が決まらない
                    d["has_waits"] = True
                ends = (None if d.get("has_waits")
                        else _run_ends_at(link, status))
                if ends is not None:
                    # 最後まで進んで終わる時刻(停止の予約がないときの画面の
                    # 「終了予定」)。周回を終えるために止まって待つ間は、装置は
                    # 動いていないので出さない(画面は周回の終え方の時刻を出す)
                    d["run_ends_at"] = ends
                if d.get("awaiting") and art is not None and art.awaits:
                    # いま止まっている AWAIT(event_index)の選択肢名を出す。
                    # 選択待ちが続く手順や、くり返しの中、入れ子の選択待ちでは、
                    # 選択待ちごとに選択肢が違う。
                    # 送った手順のコピーから取り出すので、実行中に手順のファイルを編集
                    # しても出る名前は変わらない
                    ev_idx = d.get("event_index")
                    awt = next(
                        (a for a in art.awaits if a.get("index") == ev_idx),
                        None) if ev_idx is not None else None
                    if awt and awt.get("names"):
                        d["arm_names"] = list(awt["names"])
                        # どの分岐の待ちか(名前を付けてあれば)。選択肢の
                        # ボタンの前に出す
                        d["await_name"] = awt.get("name") or ""
                        # 選択肢ごとの「選択条件」。「手動」だけを主ボタンの
                        # 見た目にする
                        d["arm_kinds"] = arm_kinds(awt)
                    # 待ちの種類。画面はバッジと待ちの欄を種類ごとに描き分ける
                    # (スクリプトの実行中を「選択待ち」と呼ばない)
                    d["await_kind"] = ("join" if awt and awt.get("join")
                                       else "script" if awt and awt.get("script")
                                       else "branch")
                    if awt and awt.get("script"):
                        d["script_cmd"] = awt["script"].get("cmd") or ""
                    # 時間切れで進む待ちなら、進む先と長さ。画面は残りの時間を
                    # 出す(待ち始めは at_since)
                    if awt and awt.get("on_timeout") and awt.get("timeout_frames"):
                        d["await_timeout"] = {
                            "arm": int(awt["on_timeout"]) - 1,
                            "frames": int(awt["timeout_frames"])}
                    if awt and awt.get("join"):
                        # 合流点の待ちは選択肢ではなく 1 行で出す。
                        # 名前は成果物のメタ、待っている相手はグループの待ち合わせが
                        # 持つ。待った長さは at_since から画面が出す
                        d["join"] = {
                            "name": awt["join"].get("name") or "",
                            "waiting_for": (join_waits.get(d["name"]) or {})
                            .get("waiting_for") or []}
                if d.get("awaiting") and not hold:
                    # 判定できなかった条件。レーンは理由を出す。自動で進む道が
                    # すべて判定できなければ、人を呼ぶ待ちになる。ほかのレーンを
                    # 待っている間は装置が停止していて、判定もしないし人も選べない
                    fails = self._condwatch().judge_failures(
                        d["name"], d.get("await_gen"))
                    if fails:
                        names = d.get("arm_names") or []
                        d["judge_errors"] = [
                            dict(f, arm_name=(names[f["arm"]]
                                              if f["arm"] < len(names) else "")
                                 or f"選択肢 {f['arm'] + 1}")
                            for f in fails]
                    # 基準画像とこの装置の映像の縦横の比が違う条件。判定は続ける
                    # ので、判定できなかった条件とは別に渡す。レーンはメッセージに出す
                    odd = self._condwatch().aspect_mismatches(
                        d["name"], d.get("await_gen"))
                    if odd:
                        d["aspect_conds"] = odd
                    # 選択肢の外周を目立たせるのは、人が選ばないと進まない待ちの
                    # ときだけである。通知を出す条件と同じもの
                    d["await_human_only"] = human_only(
                        self.project, status or {}, link.registered_id,
                        [f["arm"] for f in fails])
                    # 実行中の手順の成果物が読めないと、条件による自動の判定が
                    # 停止する。停止している理由が画面のどこにも
                    # 出ないと、いつまで選択待ちのままでも理由が分からない
                    if art is None:
                        d["auto_paused"] = True
            # 前の装置から引き継いだ実行(割り当てを解除した行と、交換した装置を
            # 割り当て直した行。Coupler.carried_lane)の停止した位置は、その実行の
            # 終わりの記録(止まり方と最後に通った区切り)と、前の装置へ送った手順の
            # コピーから出す。いまの装置は、その実行の状態を持たない。
            # 停止した後に起動し直した装置も、その実行の状態を持たない。起動してから
            # 一度も実行していない(実行中でも選択待ちでもなく、止まり方が空。
            # comm-protocol.md の STATUS)なら、その装置で最後に開始した実行の終わりの
            # 記録から出す(Coupler.ended_lane)。起動し直した後に実行していれば、
            # 止まり方があるので、上でその実行の状態から出している
            carried = self._coupler().carried_lane(link.cfg)
            if (carried is None and link.info is not None
                    and not (d.get("running") or d.get("awaiting"))
                    and d.get("stop_reason") == ""):
                carried = self._coupler().ended_lane(link.registered_id)
            if carried is not None and not (d.get("running") or d.get("awaiting")):
                try:
                    d["stopped_at"] = _carried_stop(self.project, carried)
                except positions.BAD_COPY:
                    # 送った手順のコピーが読めない形。停止した位置を出さず、ほかの欄と
                    # ほかのレーンは返す(上の停止した位置の受けと同じ)
                    _tell_bad_copy(link, carried.get("proc_id") or "")
                    d["stopped_at"] = None
            # 「見えている」と「未検出」は別の値である。
            # 応答はあるのに USB が外れている装置は、「未検出」ではないが
            # 「見えている」でもない。判定は DeviceLink が持つ。装置の STATUS
            # を広げた後に置くので、同じ名前の欄があっても上書きされない
            d["visible"], d["gone"] = link.visible, link.gone
            # 操作権を別の padcue が持っているか。所有者がいない
            # だけのときは偽である(収集が取得し直す)
            d["owned_by_other"] = link.owned_by_other
            # レーンの映像(映像ソースと切り出し範囲)。持たないレーンでは
            # 条件を判定できないので、そのことも含める
            # 手で書き換えた padcue.json の設定が読めなければ、映像はないものと
            # して、読めない理由を行に出す。読めないまま渡すと画面が壊れる
            try:
                video = validate_video(link.cfg.get("video"))
                why = ""
            except (ValueError, TypeError) as e:
                video, why = None, f"映像の設定を読み込めません: {e}"
            d["video"] = video
            d["no_video"] = video is None
            # 画像ファイルの映像は、読めるかを毎回見る。保存した後にファイルを
            # 動かしたり消したりしても、行が正常に見えないように
            src = (video or {}).get("source") or {}
            d["video_why"] = why or (FileSource(src.get("path") or "").problem()
                                     if src.get("kind") == "file" else "")
            devices.append(d)
        out["devices"] = devices
        # LAN にいるが未登録の装置。画面が「見つかった装置」として並べ、
        # 人が「使う」を押したときだけ登録される。登録できない装置(実機と模擬装置は
        # 同じプロジェクトに登録しない)には、その理由(why)を付ける。画面は「追加」と
        # 「〇〇 に割り当て」を押せなくして、理由を出す
        registered = cfg.get("devices") or []
        out["candidates"] = [
            dict(c, why=registry.register_refusal(registered, c.get("id") or ""))
            for c in getattr(self.scan, "candidates", None) or []]
        # 探索の状態。デモは探さないので含めない
        if self.scan is not None:
            out["scan"] = {"at": self.scan.last_at, "error": self.scan.error}
        # 記録している画面(記録はサーバーに 1 つだけ)。画面は、別のタブが
        # 記録している間「記録を開始」を押せなくする
        # 停止した記録は、残ったフレーム数も含める(保存していない記録を捨てる
        # 前に、新しく始める画面が確認する)
        rec = self.recorder
        out["recording"] = ({"owner": rec.owner, "active": not rec.paused,
                             "frames": rec.kept if rec.paused else 0}
                            if rec is not None else None)
        # グループ。レーンごとのグループの識別子(devices[].group)と、グループの
        # 一覧を含める。
        # 1 台のグループも含める。枠線とバッジの対象を 2 本以上のグループに絞るのは
        # 画面の判断であって、サーバーは選り分けない
        snap = self._coupler().snapshot(groups, starting, read_by_id)
        lane_group = snap.get("lane_group") or {}
        # 装置が開始を拒否したレーンは、装置が待機中のままなので状態からは
        # 分からない。サーバーが持っている理由をここで含める
        start_errors = snap.get("start_errors") or {}
        lane_notes = snap.get("lane_notes") or {}
        lane_result = snap.get("lane_result") or {}
        # 動いているグループを始めた時刻(runstate.json にあり、padcue を起動し直しても
        # 残る)
        group_started = {g.get("id"): g.get("started_at") for g in snap["groups"]
                         if g.get("active") and g.get("started_at")}
        group_of = {g.get("id"): g for g in snap["groups"]}
        for d in devices:
            gid = lane_group.get(d["name"])
            if gid:
                d["group"] = gid
            ls = (group_of.get(gid) or {}).get("linked_stop") or {}
            ran = running_lanes.get(d["name"])
            if (ran and d["name"] in (ls.get("later") or [])
                    and isinstance(ls.get("lap"), int) and not d.get("stop_at_mark")):
                # 完了に連動して、停止する周(lap)に入るのを待っている。装置にはまだ
                # 予約がないので止まる先は出さないが、その周の終わりに着く時刻は分かる
                # (画面の「終了予定」)。装置に予約があれば、その止まる先の時刻を
                # 使う(上)。
                # 待ちを含む手順でも、その周の終わりまでの道に待ちがなければ求める
                # (たどりは待ちを越えると求めない)。送った手順のコピーが読めない形
                # なら出さない
                try:
                    frames = positions.lap_end_frames(
                        self.project, ran[1], d, ls["lap"], ran[0].picked_arm(d))
                except positions.BAD_COPY:
                    _tell_bad_copy(ran[0], d.get("running_id") or "")
                    frames = None
                ends = _stop_ends_at(ran[0], d, frames)
                if ends is not None:
                    d["stop_ends_at"] = ends
            # 開始の時点で分かったメッセージと、最後の実行の結果。画面は毎秒
            # これを描く(画面の一時的なコピーを正にしない)
            d["run_notes"] = lane_notes.get(d["name"]) or []
            # この実行を始めた位置。実行中の開始位置の欄は、選んである値では
            # なくこれを出す(画面を読み込み直しても、その実行の値が出る)
            if d.get("running") or d.get("awaiting"):
                d["run_start"] = (snap.get("lane_start") or {}).get(
                    d["name"]) or {"kind": "head"}
            if lane_result.get(d["name"]):
                r = lane_result[d["name"]]
                d["last_result"] = r
                lm = d.get("last_mark") or {}
                if (r.get("why") == "finished" and d.get("stopped_at")
                        and not (d.get("running") or d.get("awaiting"))
                        and r.get("mark") == [lm.get("index"),
                                              list(lm.get("counters") or [])]):
                    # 周回を終えて、途中の区切り(次の周の先頭)で完了した。
                    # 完了した実行に停止した位置はない
                    d["stopped_at"] = None
            # 実行を始めた時刻は、収集が実行中を初めて見た時刻なので、padcue を
            # 起動し直すと起動し直した時刻になる。グループの記録がこの実行のもの
            # (終わりの結果がまだなく、送った中身が装置の実行中の中身と同じ)なら、
            # グループを始めた時刻を使う(周回を終えるための続きが、元の実行の
            # 開始時刻を引き継ぐのと同じ扱い)
            g_at = group_started.get(gid)
            lane_run = _last_run_in(groups, d["id"])
            if (g_at and d.get("run_started_at") and "last_result" not in d
                    and d.get("running_id")
                    and _last_proc_in(groups, d["id"]) == d["running_id"]
                    and not proto.later_run(lane_run, proto.run_of(d))):
                # 同じ手順でも、レーンの実行の後に別の道具が始め直した実行には当てない
                d["run_started_at"] = min(d["run_started_at"], g_at)
            done = (snap.get("lane_done") or {}).get(d["name"])
            if done and (d.get("running") or d.get("awaiting")):
                # 周回を終えるために、周回数のくり返しの後ろから続けている。
                # 変換に周はないが、元の実行の周回数と何周目かを出す
                d.pop("nolaps", None)
                d["lap"], d["laps_total"] = done[0], (
                    "infinite" if done[1] is None else done[1])
            why = start_errors.get(d["name"])
            if why:
                d["start_error"] = why
            why = lap_end_errors.get(d["name"])
            if why:
                d["lap_end_error"] = why
            # 応答がなくて送れなかった人の止め方を、接続が戻ったら送る(coupler の
            # _keep_stop)。未検出のレーンの文が示す
            later = (snap.get("lane_stop_later") or {}).get(d["name"])
            if later:
                # 予約で止まって待っていたレーンは、どちらの止め方でも、戻ったらその
                # レーンの実行が終わる(装置へは何も送らない。coupler の
                # _send_kept_stop)。押したときの応答と同じく、停止すると返す
                d["stop_later"] = ("immediate" if d.get("lap_end_hold") else later)
        # 周回の終え方(「区切りで停止」の ▾ のメニュー)。選んで、まだ続きから
        # 始め直していない間は、レーンの周回数を選んだ周回数で出し、その予約を
        # 「区切りで停止」の予約に見せない(続きから始め直すための予約である)
        menus = self._coupler().lap_ends.menus(groups, read_by_id,
                                               starting["restarting"])
        for d in devices:
            menu = menus.get(d.get("group") or "")
            if not menu or not menu.get("pending"):
                continue
            if d["name"] not in menu["pending_laps"]:
                # 待つ間にレーンの止め方で終え方から外したレーン(LapEnds.stop_of)は、
                # 元の周回数のまま停止する。選んだ周回数も終え方の予約も見せない
                continue
            d["laps_total"] = menu["pending_laps"][d["name"]] or "infinite"
            d["lap_end_pending"] = True
            if d.get("stop_at_mark") == lapend.MASK:
                d["stop_at_mark"] = 0
                d.pop("stop_target", None)
                d.pop("stop_ends_at", None)
        for g in snap["groups"]:
            if g.get("id") in menus:
                g["lap_end"] = menus[g["id"]]
        self._lap_end_ends(devices, running_lanes, groups)
        out["groups"] = snap["groups"]
        # プリセット(グループのひな形)。1 台でも返す。グループの下限は 1 台
        # なので、台数で出し分ける理由がない。名前はファイルの名前である
        fmts = []
        for fname in self.project.presets.names():
            try:
                fmts.append({"name": fname,
                             "lanes": self.project.presets.load(fname)["lanes"]})
            except ValueError as e:
                # 読めない理由(PresetStore.load が画面に出す文にしてある)
                fmts.append({"name": fname, "error": str(e)})
        out["presets"] = fmts
        # スリープの抑止ができていないこと。長時間の実行の途中でスリープに入ると、
        # 装置は動き続けるのに監視と記録が止まる。画面は見出しのバッジで出す
        if self.sleepguard is not None:
            out["sleep_guard"] = self.sleepguard.status()
        return out

    def _figure(self, d: dict, link, art, lane_dev: str, hold: bool,
                run: float = 0.0, groups: list | None = None):
        """実行中のレーンの今の図(positions.Figure)を返す。図の変換結果がなければ None。

        図は、今の周と、その周に選んだ選択肢で描く。選んだ選択肢は、前に描いた図の分と、
        レーンの記録に残した分と、PC が送った選択と、装置の状態から取り出す
        (positions.picks_now)。
        周ごとに図が変わる実行(周で選ばれ方が変わる分岐があるか、今の周に選んだ
        選択肢がある)では、次の周の図のリビジョンと、その図の上の周の頭と終わりを
        d の next_lap に含める。画面は、装置を読み直すまでの間に線が周の終わりを
        越えたら、先に受け取ったその図へ替え、線を図と同時に次の周へ移す。次の周の
        図は、周回数のくり返しの中の分岐を、まだ選んでいない形で描く。

        送った手順のコピーが読めない形だと例外が出る(positions.BAD_COPY。呼ぶ側が
        受ける)。
        """
        kept = self._coupler().lane_figure(lane_dev, art.hash, groups,
                                           proto.run_of(d))
        lap = positions.figure_lap(art, d, hold, kept["done"])
        # 前に描いたこの実行の図の選択も使う(DeviceLink.drawn_picks)。レーンの記録に
        # 残した分と同じ通過なら、残した分を使う。実行は、状態と一緒に読んだ開始時刻
        # run で見分ける。図を作っている間に新しい実行が始まったら、残さない
        # (DeviceLink.keep_drawn)
        drawn_proc, drawn_run, drawn = link.drawn_picks
        same = run and drawn_proc == art.hash and drawn_run == run
        earlier = [*(drawn if same else ()), *kept["picks"]]
        picks = positions.picks_now(self.project, art, d, lap, lane_dev,
                                    earlier, link.picks_sent, link.seen_states)
        link.keep_drawn(art.hash, run, picks)
        rows = positions.figure_rows(self.project, art, lane_dev, kept["rows"])
        fig = positions.figure_of(self.project, art, lane_dev, lap, picks, rows)
        if fig is None:
            return None
        lm = d.get("last_mark") or {}
        inside = bool(art.lap_map) and not positions.past_laps(art.lap_map,
                                                               lm.get("index"))
        last = isinstance(art.laps, int) and lap >= art.laps
        varies = (not positions.laps_alike(art)
                  or any(p.get("lap") is not None for p in picks))
        if inside and not hold and not last and varies:
            ahead = positions.figure_of(
                self.project, art, lane_dev, lap + 1,
                [p for p in picks if p.get("lap") is None], rows)
            span = positions.span_on(ahead) if ahead is not None else None
            if span is not None:
                d["next_lap"] = {"rev": ahead.rev, "span": span}
        return fig

    def _put_position(self, d: dict, link, art, lane_dev: str, hold: bool,
                      fig) -> None:
        """レーンの状態 d に、「区切りで停止」の止まる先とそこで停止する時刻、図の上の
        現在位置(線)を書く。art は装置へ送った手順のコピー、fig は今の図である
        (_figure。なければ、線と、止まる先の図の上の位置は出さない)。

        送った手順のコピーが読めない形だと例外が出る(positions.BAD_COPY。呼ぶ側が
        受ける)。
        """
        # 選択肢が 2 つ以上の選択待ちを抜けた直後は、PC が選択を送っていれば、止まる
        # 先をその選択肢でたどる
        pick = link.picked_arm(d)
        d["stop_target"] = positions.stop_target(self.project, art, d, lane_dev, pick,
                                                 fig)
        ends = _stop_ends_at(link, d, (d["stop_target"] or {}).get("frames"))
        if ends is not None:
            # 停止の予約で停止する時刻。止まる先までのフレーム数を、最後に通った区切りの
            # 時刻に足す。図の上の位置によらない
            d["stop_ends_at"] = ends
        if fig is None:
            return
        if d.get("running") or d.get("awaiting"):
            # 図の上の現在位置。装置の経過フレーム数は、開始位置から
            # 始めると 0 から数え直しになるので図と合わない。最後に
            # 通った区切りと、そこからの経過で出す
            lm = d.get("last_mark") or {}
            at = (positions.frame_of(self.project, art, lm.get("index"), lane_dev,
                                     lm.get("counters"), fig) if lm else None)
            if at is not None:
                d["at_frame"] = at
                if positions.arm_open(art, d, fig):
                    # 選択肢が 2 つ以上の選択待ちを抜けたのに、どの選択肢へ進んだかが
                    # まだ分からない。図はその選択待ちを枠だけで描いているので、線を
                    # 枠の所で待たせる(次の区切りを読むと、続きの上に置ける。選択肢が
                    # 分かれば、その中身で描き直して進める)
                    d["at_open"] = True
                    d["at_hold"] = True
                else:
                    d["at_since"] = link.mark_at
            elif not lm and isinstance(d.get("frames_elapsed"), int):
                # まだ区切りを 1 つも通っていない(くり返しの直後から
                # 始めた実行や、周回数のくり返しの前に区切りのない手順)。
                # 位置は開始位置で、始めた時刻からの経過で進める。始めた
                # 時刻は、装置の経過フレーム数を、それを読めた時刻から引いて
                # 出したもの(DeviceLink.frame0_at)である。今の時刻から引くと、
                # 装置を読めない間に始めた時刻だけが先へずれ、画面の線(最後に
                # 読めた時刻の少し先で止める)が戻る
                at = positions.run_start_frame(self.project, art, lane_dev, fig)
                if at is not None and link.frame0_at:
                    d["at_frame"] = at
                    d["at_since"] = link.frame0_at
            # 最後に通った区切りを図の上に置けないとき(周ごとの図を作れない実行で、
            # 1 周目の図にない選択肢の中にいるなど)は、線を出さない
            span = (positions.lap_span(self.project, art, d, lane_dev, fig)
                    if "at_frame" in d and not hold else None)
            if span is not None:
                # 図の上の周の頭と終わり。画面は、装置を読み直すまでの間に
                # 線が周の終わりを越えたら、周の頭へ戻して描く
                d["lap_span"] = span
        if hold and "at_frame" in d:
            # 停止しているので、線を進めない。周の境で停止したときは、停止した
            # のは前の周の終わりなので、図の周の終わりに置く(装置が停止した
            # 次の周の先頭に置くと、次の周に入って固まったように見える)
            end = positions.lap_end_frame(self.project, art, d, lane_dev, fig)
            if end is not None:
                d["at_frame"] = end
            d["at_hold"] = True
            d.pop("at_since", None)

    def _lap_end_ends(self, devices: list, running_lanes: dict,
                      groups: list | None = None) -> None:
        """周回の終え方を選んでから続きを始めるまでのレーン(lap_end_pending)に、終わる
        時刻(lap_end_ends_at。画面の「終了予定」)を書く。

        レーンごとに、予約で停止する区切りとそこに着く時刻を求める。先に止まって
        ほかのレーンを待つレーンは、止まった区切りと、それを通った時刻である。グループの
        全レーンが揃った時刻から続きの長さを数えるのは LapEnds.ends_at である。待ちを
        含む手順でも、止まる所までと続きの道に待ちがなければ求める(たどりは待ちを
        越えると求めず、続きに待ちがあれば続きの長さを求めない)。
        未検出のレーンは含めない(そのグループには出さない)。続きはそのレーンが戻るまで
        始まらず、いつ戻るかも、いつ止まる所に着くかも分からない。最後に読めた状態から
        出すと、その時刻を過ぎても出し続ける。送った手順のコピーが読めない形のレーンも
        含めない(LapEnds.ends_at も、そのグループには出さない)。
        """
        stops = {}
        for d in devices:
            ran = running_lanes.get(d["name"])
            if not d.get("lap_end_pending") or ran is None or d.get("gone"):
                continue
            link, art = ran
            period = (link.info.frame_period_ns if link.info
                      else proto.DEFAULT_FRAME_PERIOD_NS) / 1e9
            if d.get("lap_end_hold"):
                lm = d.get("last_mark")
                at = link.passed_at(lm) if lm else None
            else:
                try:
                    lm, frames = positions.lap_end_stop(self.project, art, d,
                                                        link.picked_arm(d))
                except positions.BAD_COPY:
                    _tell_bad_copy(link, d.get("running_id") or "")
                    continue
                at = _stop_ends_at(link, d, frames)
            if lm and at:
                stops[link.registered_id] = (lm, at, art, period)
        if not stops:
            return
        ends = self._coupler().lap_ends.ends_at(stops, groups)
        for d in devices:
            if d.get("id") in ends:
                d["lap_end_ends_at"] = ends[d["id"]]

    @staticmethod
    def _wait_first_collect(links, timeout: float = 3.5) -> None:
        """サーバーを起動したときからある装置の、最初の収集が終わるまで少し待つ。

        待たないと、画面の最初の 1 秒だけ全装置が「未接続」に見えてしまう
        (収集は非同期なので、起動直後はまだ 1 度も返ってきていない)。

        待ちは、装置への接続の待ち(3.0 秒)より長い 3.5 秒にする。いない装置が
        1 台あっても、その間に 1 巡が終わる。後から登録した装置や、改名で作り
        直した装置の最初の収集は待たない。応答しない装置を改名すると、その装置の
        接続の待ちの間、すべての装置の表示が更新されなくなる。
        """
        end = time.monotonic() + timeout
        while (any(link.from_start and link.at == 0 for link in links)
               and time.monotonic() < end):
            time.sleep(0.05)
