"""実行の記録(logs.jsonl と古い世代)の読み書き。

装置のログは、受け取ったと知らせると装置から消える(リングバッファー。
comm-protocol.md の LOGS)。受け取った分をプロジェクト直下の logs.jsonl に、1 行 1 件の
JSON で追記して、あとから見返せるようにする。受け取った時刻(PC の時計)は、追記する
ときに付ける。

状態が変わったときに装置が書く行(kind が STATE。「実行中 → 待ち」のように前と後の状態を
持つ)を「状態の行」と呼び、後の状態で「異常停止に移った状態の行」のように言う。
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from typing import NamedTuple

from .files import remove_file, replace_file, write_file
from .proto import STATE_ERROR


def _parse(line: str) -> dict | None:
    """ログの 1 行を読む。JSON のオブジェクトでなければ None(手で書き換えた行)。

    読めない行は読み飛ばす。数や配列の行をそのまま返すと、読む側の .get で
    落ちて、ログの画面の全体が開けなくなる。
    """
    try:
        got = json.loads(line)
    except ValueError:
        return None
    return got if isinstance(got, dict) else None


def stop_line(e: dict) -> bool:
    """装置が停止したときに書く行か。止まり方の行(firmware の app_runend.h。手順
    データの異常で停止したときは、停止の行の代わりに ENGINE_FAULT を書く)と、異常
    停止に移った状態の行である。待機中に移った状態の行は、画面が止まり方の行との時刻の
    近さで畳むので、ここには入れない(_before_the_end)。"""
    kind = e.get("kind")
    return (kind in ("RUN_DONE", "RUN_ABORT", "ENGINE_FAULT")
            or (kind == "STATE" and e.get("b") == STATE_ERROR))


def lines_up_to_the_stop(entries: list[dict]) -> list[int]:
    """装置が停止する前と停止したときに書いた装置の行の番号。終わりの時刻より後ろに
    しない行である(_before_the_end。収集が見積もった停止の時刻で書く行も同じ。
    devicepool の DeviceLink._stamp_stop)。

    装置の記録の順で、最後の停止したときの行(stop_line)までの行である。最後の行が
    装置の終わりの行(RUN_DONE・RUN_ABORT)なら、それは入れない。画面は PC の終わりの
    行に畳み、その時刻と、続いて書く待機中に移った状態の行の時刻の近さで、状態の行も
    畳む。PC の行は入れない。
    """
    last = max((i for i, e in enumerate(entries) if stop_line(e)), default=-1)
    return [i for i, e in enumerate(entries)
            if (i < last or (i == last
                             and e.get("kind") not in ("RUN_DONE", "RUN_ABORT")))
            and not str(e.get("kind", "")).startswith("PC_")]


def _before_the_end(entries: list[dict]) -> list[dict]:
    """1 周のまとまりの行のうち、装置が停止する前と停止したときに書いた装置の行の
    時刻を、実行の終わり(PC_RUN_END)の時刻より後ろにしない。

    装置の行の時刻は、PC が受け取った時刻である(装置の記録の t_ms は装置の起動からの
    時計で、PC の時計に変換する手がかりを装置は返さない)。終わりの行の時刻は、装置が
    停止した時刻なので、停止する直前に装置が書いた行(条件で自動選択した後の
    「待ち → 実行中」など)を受け取るのが、それより後になることがある。そのまま時刻で
    並べると、完了の後に動き出したように読める。異常停止では、停止した原因の行
    (「実行中にエラーが発生しました」)と異常停止に移った状態の行が終わりの行の後ろに
    並び、停止した後に原因が起きたように読める。
    停止する前と停止したときに書いた行は、装置の記録の順で、最後の停止したときの行
    (stop_line)までの行である。時刻を揃えた行は、書いた順(原因、状態、終わり)に
    並ぶ。それより後の装置の行(停止した後に抜いた USB など)は、受け取った時刻のまま
    終わりの行の後ろに並ぶ。最後の行が装置の終わりの行(RUN_DONE・RUN_ABORT)なら、
    それは動かさない。画面は PC の終わりの行に畳み、その時刻と、続いて書く待機中に
    移った状態の行の時刻の近さで、状態の行も畳む。異常停止に移った状態の行が続く
    ときは、2 つを同じ時刻に揃える。まとまりは終わりの行で閉じるので、まとまりの
    中の行はどれも終わりの行より前に書かれている。
    """
    end = next((e for e in entries if e.get("kind") == "PC_RUN_END"), None)
    if end is None or not isinstance(end.get("at"), (int, float)):
        return entries
    held = set(lines_up_to_the_stop(entries))
    out = []
    for i, e in enumerate(entries):
        at = e.get("at")
        if (i in held
                and isinstance(at, (int, float)) and at > end["at"]):
            # 時刻も終わりの時刻にする(装置の行は停止する前に起きている。受け取った
            # 時刻のまま出すと、時刻の並びが前後する)
            e = dict(e, at=end["at"])
        out.append(e)
    return out


def _take_later(prev: dict, lap: dict, at) -> bool:
    """前のまとまり(prev)にある、その周に入った時刻 at より後の行を、周の行(lap)の
    まとまりへ移す。移したかを返す。

    周の行の時刻は、装置がその周の境を通った時刻である(coupler の _note_laps)。行は
    監視が周の境を読んでから書くので、通った後、読むまでの間に書かれた行
    (装置の行など)は前のまとまりに入っている。監視が読む前に停止した実行では、終わりを
    書く直前に書く(coupler の _note_missed_lap)。停止したときの装置の行は、それより
    先に書かれているので、書いた順のままだと前の周の行に入り、停止した理由が終わりの
    行と別の周に分かれる。前のまとまりの周の行そのものは動かさない。
    """
    if not isinstance(at, (int, float)):
        return False
    later = [x for x in prev["entries"]
             if x.get("kind") != "PC_LAP" and isinstance(x.get("at"), (int, float))
             and x["at"] > at]
    if not later:
        return False
    ids = {id(x) for x in later}
    prev["entries"] = [x for x in prev["entries"] if id(x) not in ids]
    lap["entries"].extend(later)
    return True


def _tail(path, n: int) -> list[str]:
    """ファイルの最後の n 行。後ろから少しずつ読むので、読む量は n に比例し、
    ファイルの大きさによらない。"""
    if n <= 0:
        return []
    with path.open("rb") as f:
        f.seek(0, 2)
        pos = f.tell()
        buf = b""
        # n 行の前の改行まで読む(先頭の行が途中から始まらない)
        while pos > 0 and buf.count(b"\n") <= n:
            step = min(pos, 1 << 16)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf
    # 区切った所で文字が分断されても、分断されるのは捨てる先頭の行だけである
    return buf.decode("utf-8", errors="replace").splitlines()[-n:]


class LapFold:
    """記録を 1 行 1 周にまとめる畳み込み(RunLog.group_by_lap の中身)。続きの記録を
    後から足せる。

    画面のログの欄は、毎秒、前に受け取った後の新しい行だけを受け取る(RunLog.read_view)。
    新しい行だけを別にまとめると、周の途中の行が前に受け取った周のまとまりに入らず、
    前のまとまりの行を周の行のまとまりへ移せない。そこで、まとめた途中の状態(装置ごとの
    今のまとまりと、直前の行が入ったまとまりと、周の行がまだない実行の周を持たない
    まとまり)を持ち、続きの行をそこから足す。持つのは、この先の行が変えうるまとまり
    だけである(ほかは、もう変わらない)。まとまりには、作った順の番号(id)を付ける。
    続きの応答は、それで変わったまとまりを指す。
    """

    def __init__(self):
        self._cur: dict[str, dict] = {}   # 装置ごとの今のまとまり
        self._last: dict | None = None    # 直前の行が入ったまとまり
        # 装置ごとの、開始の行を書いた後、まだ周の行のない実行の、周を持たないまとまり
        # (開始の行のまとまりから、作った順)。その時刻より後の行を、最初の周の行の
        # まとまりへ移す(feed)
        self._loose: dict[str, list[dict]] = {}
        self._next = 0

    def feed(self, entries) -> dict:
        """記録を足し、変わったまとまりを返す。

        返すのは {"made", "grew", "changed", "gone"} である。made は新しいまとまり
        (作った順)、grew は行を足しただけのまとまりの id と足した行、changed は
        それ以外に変わった(行がほかのまとまりへ移った、実行の終わりの行が入って並びと
        時刻が変わりうる)まとまり、gone は行がすべて移って空になり消えたまとまりの id
        である。
        まとまりは生の形で、表示の形には shown で変換する。

        まとめ方: 区切りになるのは、周が進んだ記録(PC_LAP)と実行の終わり(PC_RUN_END)
        である。レーンごとにまとめるので、2 台のグループでも互いの周が混ざらない。
        実行の外の出来事(起動、接続、開始の拒否)は、周の番号を持たないまとまりに
        入る。画面はそれを 1 行ずつ並べるので、そのまとまりは続けて書かれた行だけを
        集める(間にほかの装置の行が入ったら閉じる)。閉じないと、後の行が前の
        まとまりへ戻って入り、ほかの装置の行より上に出る。その周に入った時刻(装置が
        周の境を通った時刻)より後の同じ装置の行は、前の周のまとまりから周の行の
        まとまりへ移す(_take_later)。実行の最初の周の行のまとまりへは、開始の行から
        後の周を持たないまとまりからも、閉じたものを含めて移す。周の行は時刻を後から
        決めて書くことがあり(監視が初めて読む前に通り過ぎた周。coupler の
        _note_laps)、その前に書いた行(周回数の変更など)が、ほかの装置の行で閉じた
        まとまりに入っている。
        どの行がその周に入るかは、書いた順によらず、周の行の時刻で決まる。
        """
        cur = self._cur
        loose = self._loose
        made: list[dict] = []
        grew: dict[int, list] = {}
        changed: dict[int, dict] = {}
        gone: list[int] = []
        new_ids: set[int] = set()

        def touched(g: dict, e: dict | None) -> None:
            """g が変わった。e は足した行(行がほかへ移ったときは None)。"""
            if g["id"] in new_ids:
                return                       # 新しいまとまりは、そのまま全部を返す
            if e is None or e.get("kind") == "PC_RUN_END" or g["id"] in changed:
                changed[g["id"]] = g
                grew.pop(g["id"], None)
            else:
                grew.setdefault(g["id"], []).append(e)

        for e in entries:
            dev = e.get("dev", "")
            # 開始の行は、前の実行の周のまとまりを閉じる。前の実行の終わりの記録が
            # なくても、次の実行の「1 周目」が前の「1 周目」に混ざらないようにする。
            # 周を持たないまとまり(起動・接続などの行)には、そのまま加わる
            if (e.get("kind") == "PC_SET_START" and dev in cur
                    and cur[dev].get("lap") is not None):
                cur.pop(dev, None)
            g = cur.get(dev)
            if g is not None and g.get("lap") is None and g is not self._last:
                g = None
            if (e.get("kind") == "PC_LAP" and g is not None
                    and g.get("lap") == e.get("a")):
                # 同じ周を二度書いた行(サーバーを起動し直すと起きる)。番号が
                # 同じなら同じ周なので、同じまとまりへ入れる。分けると同じ
                # 「3 周目」の行が 2 つ並ぶ
                g["entries"].append(e)
                touched(g, e)
                self._last = g
                continue
            if g is None or e.get("kind") == "PC_LAP":
                new = {"id": self._next, "dev": dev,
                       "lap": (e.get("a") if e.get("kind") == "PC_LAP" else None),
                       "entries": []}
                self._next += 1
                new_ids.add(new["id"])
                if e.get("kind") == "PC_LAP":
                    # 移す元は、作った順に見る(移した行が書いた順に並ぶ)
                    srcs = list(loose.pop(dev, []))
                    if dev in cur and cur[dev] not in srcs:
                        srcs.append(cur[dev])
                    for src in srcs:
                        if not _take_later(src, new, e.get("at")):
                            continue
                        touched(src, None)
                        if not src["entries"]:
                            if src["id"] in new_ids:
                                made.remove(src)
                            else:
                                gone.append(src["id"])
                                changed.pop(src["id"], None)
                elif dev in loose:
                    loose[dev].append(new)
                g = new
                cur[dev] = g
                made.append(g)
            else:
                touched(g, e)
            g["entries"].append(e)
            self._last = g
            if e.get("kind") == "PC_SET_START":
                # 周回数のくり返しより後ろから始めた実行(nolaps)は、周の行を書かない
                if e.get("nolaps"):
                    loose.pop(dev, None)
                else:
                    loose[dev] = [g]
            if e.get("kind") == "PC_RUN_END":
                cur.pop(dev, None)
                loose.pop(dev, None)
        return {"made": made, "grew": grew, "changed": list(changed.values()),
                "gone": gone}

    def open_ids(self) -> list[int]:
        """この先の行が変えうるまとまりの id。

        装置ごとの今のまとまり(行が足されるか、行が周の行のまとまりへ移りうる)と、
        周の行がまだない実行の周を持たないまとまり(行が最初の周の行のまとまりへ
        移りうる)である。
        """
        out = [g["id"] for g in self._cur.values()]
        return out + [g["id"] for gs in self._loose.values() for g in gs
                      if g["id"] not in out]

    @staticmethod
    def shown(g: dict, with_id: bool = False) -> dict:
        """まとまりを表示の形にする(コピーを作る。続きを足す状態は変えない)。

        まとまりの中は時刻の順に並べる。実行の終わり(PC_RUN_END)は、装置が
        停止したと確認してから書くので、停止した後の装置の行より後ろに書かれる
        (時刻は停止した時刻である)。停止する前の装置の行は、終わりの行より前に
        置く(_before_the_end)。
        """
        entries = _before_the_end(list(g["entries"]))
        entries.sort(key=lambda x: x.get("at") or 0)
        out = {"dev": g["dev"], "lap": g["lap"], "at": entries[0].get("at"),
               "entries": entries}
        if with_id:
            out["id"] = g["id"]
        return out


def _log_dev(line: str) -> str:
    """ログの 1 行の装置 ID。読めない行と、装置を持たない行は空。

    壊れた行は、どの装置の行かが分からないので、装置を指定した削除では残す。
    """
    got = _parse(line)
    return str(got.get("dev") or "") if got else ""


class LogPage(NamedTuple):
    """記録を読んだ範囲(RunLog.read_page)。"""
    entries: list[dict]     # 古い順(表示の並び)。読めない行は除く
    more: bool              # それより古い記録がまだ残っているか
    count: int              # 読んだ行の数(読めない行も数える)
    mark: str               # 読んだときの書き込みの印。次に読むときに渡す


class RunLog:
    """プロジェクトの実行の記録。"""

    # 書き込み中のファイルがこの行数を超えたら、世代を分けて書き始める
    LOG_KEEP = 5000

    # まとめた途中の状態を覚えておく数(画面のタブごとに 1 つ使う)。超えたら古いものから
    # 忘れる。忘れた印で読みに来た欄は、範囲の全体を読み直す
    VIEWS_KEEP = 8

    # 世代の合計の上限。1 行はおよそ 200 バイトなので、100MiB は
    # 約 50 万行にあたり、1 時間あたり 480 件の見積りで約 1000 時間分になる
    LOG_TOTAL_MAX = 100 * 1024 * 1024

    def __init__(self, project):
        self.project = project
        self.root = project.root
        # 前回の整理からの追記行数。上限ぶん溜まったときだけ整理する
        # (最初の 1 回は必ず通して、再起動をまたいで溜まった行を整理する)
        self._since_trim = self.LOG_KEEP
        # 古い世代を消したことがあるか。一度立ったら消すまで下がらないので、
        # 立ってからはファイルを見に行かない(dropped)
        self._dropped_seen = False
        # 書き込みの印(mark)。この起動で書いた行の数と、起動ごとに変わる名前である。
        # 印の後に書いた行の数が分かるので、読む側は、読み込んだ範囲の古い端を
        # 動かさずに新しい行だけを足せる(read_page)。名前が違う印(起動し直す前の
        # 印)からは数えない。ほかのプロセスが記録を書いたのを見たときも名前を替える
        # (_note_others)
        self._run = secrets.token_hex(4)
        self._written = 0
        # 書き込み中のファイルの、このプロセスが最後に書いたか見た後の大きさ(バイト)。
        # 今の大きさと違えば、ほかのプロセス(操作画面のサーバーと別に動かした
        # padcue logs --host など)が書いた(_note_others)
        self._size = self._size_now()
        # 画面のログの欄ごとの、まとめた途中の状態(read_view)。応答の印(view) ->
        # (その応答の書き込みの印, LapFold, 消した回数)。印は 1 回だけ使える
        self._views: dict[str, tuple[str, LapFold, int]] = {}
        self._views_lock = threading.Lock()
        # ログの行を消した回数(clear と、古い世代を消したとき)。書き込みのロックの中で、
        # 消し終えてから数える。まとめた途中の状態は、覚えた後に消した行を持ちうるので、
        # 回数が変わったら使わない(read_view は範囲の全体を読み直す)
        self._cleared = 0

    def path(self):
        return self.root / "logs.jsonl"

    # 書き込みは 1 つずつ行う。装置ごとの収集スレッドが並行して追記すると、追記と
    # 整理(全読み・名前の変更)が競合して行が消える。呼び出し元(GUI)の lock に
    # 頼らず、ここで守る。ロックはクラスで 1 つなので、同じフォルダーを別の RunLog
    # が書いても並ぶ
    _log_write_lock = threading.Lock()

    def append(self, entries: list[dict], dev: str = "") -> None:
        """ログを追記する。dev は装置の ID(どの装置の記録かを区別する。
        保存キーは改名に耐える id、表示時に名前へ解決する。P23)。"""
        if not entries:
            return
        with self._log_write_lock:
            self._write(entries, dev)
            # 毎回 _trim を呼ぶと、追記のたびに書き込み中のファイル(最大 5000 行、
            # 約 1MiB)を全部読んで行数を数えることになる。しかもこれは書き込みの
            # ロックの内側なので、各装置の収集スレッドがその全読みで直列化する。
            # 上限ぶん溜まったときだけ整理する
            self._since_trim += len(entries)
            if self._since_trim >= self.LOG_KEEP:
                self._since_trim = 0
                notes = self._trim()
                if notes:
                    # 整理そのものを記録する行。ここから append を呼ぶと同じロックを
                    # 取得し直してデッドロックするので、書き込みだけを呼ぶ
                    self._write(notes)

    def _size_now(self) -> int:
        """書き込み中のファイルの今の大きさ(バイト)。ないときは 0、読めなければ -1。"""
        try:
            return self.path().stat().st_size
        except FileNotFoundError:
            return 0
        except OSError:
            return -1

    def _note_others(self) -> None:
        """書き込み中のファイルを、ほかのプロセスが変えたかを見る(書き込みのロックを
        持っている側から呼ぶ)。

        印の後の行の数は、このプロセスが書いた行で数える(_added_since)。ほかの
        プロセスが追記した行、世代を分けた、消したことは、その数に入らない。そこで、
        このプロセスが最後に書いたか見た後の大きさと今の大きさが違えば、印の名前を
        替える。それまでの印(画面の欄が持つ印と、まとめた途中の状態)からは続きを
        読まず、範囲の全体を読み直させる(read_page、_read_on)。
        """
        size = self._size_now()
        if size != self._size:
            self._size = size
            self._run = secrets.token_hex(4)

    def _write(self, entries: list[dict], dev: str = "") -> None:
        """ログを追記する(書き込みのロックを持っている側から呼ぶ)。"""
        self._note_others()
        now = time.time()
        lines = []
        for e in entries:
            d = dict(e)
            # PC が受け取った時刻(UNIX 秒)。書く側が時刻を決めていればそれを使う
            d.setdefault("at", now)
            if dev and "dev" not in d:
                d["dev"] = dev
            lines.append(json.dumps(d, ensure_ascii=False))
        with self.path().open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self._written += len(lines)
        self._size = self._size_now()

    def _trim(self) -> list[dict]:
        """いまのファイルが上限を超えたら、世代を分けて書き始める。

        返すのは、記録そのものへ追記する行である(なければ空)。

        捨てない。書き込み中のファイルが LOG_KEEP 行を超えたら、その時点の
        日時を付けた名前へ変え、新しいファイルから書き始める。世代の合計が
        LOG_TOTAL_MAX を超えたときだけ、最も古い世代を消す。

        すべてを読み直して書き戻す形にしないのは、記録が増えるほど整理の費用が
        比例して増えるからである。名前を変えるだけなら、大きさに依らない。
        """
        p = self.path()
        self._note_others()
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        if len(lines) <= self.LOG_KEEP:
            return []
        # 名前の順が作った順になるよう、時刻の後ろの番号は桁を揃えて最初の
        # 世代から付ける(同じ秒に 2 つ分けたとき、番号のない名前と付けた名前では、
        # 「-」が「.」より前なので後の世代が前に並ぶ)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        n = 0
        dst = self.root / f"logs-{stamp}-{n:02d}.jsonl"
        while dst.exists():
            n += 1
            dst = self.root / f"logs-{stamp}-{n:02d}.jsonl"
        try:
            # 名前を変える。ほかのスレッドが記録を読んでいる瞬間は Windows が拒否
            # するので、replace_file で少し待ってやり直す
            replace_file(p, dst)
        except OSError:
            return []
        # 自分で世代を分けた。新しいファイルはまだない(ほかのプロセスが書いたとは
        # 見ない。行は数で読むので、世代を分けても続きを読める)
        self._size = 0
        return self._drop_old_generations()

    def _drop_old_generations(self) -> list[dict]:
        """世代の合計が上限を超えたら、古い世代から消す。

        最も新しい世代は残す。1 つの世代だけで上限を超えていても、直前の
        記録まで消えると「さらに読む」で何もさかのぼれなくなる。
        """
        gens = self.generations()[:-1]
        total = 0
        for f in gens:
            try:
                total += f.stat().st_size
            except OSError:
                pass
        dropped = False
        for f in gens:
            if total <= self.LOG_TOTAL_MAX:
                break
            try:
                total -= f.stat().st_size
                remove_file(f)
                dropped = True
            except OSError:
                pass
        if not dropped:
            return []
        # 画面の欄が読み込んでいた古い行も消えた。どの欄も次は範囲の全体を読み直す
        # (read_view)。続きだけを受け取ると、消えた行が欄に残る
        self._cleared += 1
        # 消えた記録より古い成果物は、もうどの行からも参照されない
        oldest = self.oldest_at()
        self.project.sent.drop_old(oldest)
        # 捨てた事実を記録そのものに残す。別の状態ファイルを持たなくても、
        # 記録を読めば「ここより古い分はない」と分かる
        self._dropped_seen = True
        return [{"kind": "PC_LOG_DROPPED", "oldest": oldest}]

    def oldest_at(self) -> float:
        """残っている記録の最も古い時刻(UNIX 秒)。なければ 0。"""
        files = [*self.generations(), self.path()]
        for f in files:
            try:
                with f.open(encoding="utf-8") as fh:
                    for line in fh:
                        got = _parse(line)
                        try:
                            return float(got.get("at", 0.0))
                        except (AttributeError, TypeError, ValueError):
                            continue      # 読めない行か、時刻が数でない行
            except OSError:
                continue
        return 0.0

    def generations(self) -> list:
        """古い世代のファイル(古い順)。書き込み中のものは含まない。"""
        return sorted(self.root.glob("logs-*.jsonl"))

    def dropped(self) -> bool:
        """古い世代を消したことがあるか。

        消したときは、名前を変えた直後の新しいファイルの 1 行目に
        PC_LOG_DROPPED を書く(_drop_old_generations)。だから、残っている
        どれかのファイルの 1 行目がその種別なら、消したことがある。状態を
        別のファイルに持たないのは、記録を読めば分かるからである。

        読むのは各ファイルの 1 行目だけである。消したことが分かったら覚えて
        (_dropped_seen)、次からは見に行かない。消したことがない間は、呼ぶたびに
        全部の世代の 1 行目を開く(世代は、消し始めるまでに約 100 個溜まる)。
        """
        if self._dropped_seen:
            return True
        for f in (*self.generations(), self.path()):
            try:
                with f.open(encoding="utf-8") as fh:
                    head = fh.readline()
            except OSError:
                continue
            if (_parse(head) or {}).get("kind") == "PC_LOG_DROPPED":
                self._dropped_seen = True
                return True
        return False

    def read(self, limit: int = 2000) -> list[dict]:
        """古い順(表示の並び)で、新しい方から limit 件を返す。"""
        return self.read_page(limit).entries

    def mark(self) -> str:
        """いまの書き込みの印(read_page に渡す)。"""
        return f"{self._run}:{self._written}"

    def _added_since(self, mark: str) -> int | None:
        """印 mark の後に書いた行の数。この起動の印でなければ None。"""
        run, _, n = (mark or "").partition(":")
        if run != self._run or not n.isdigit():
            return None
        return max(0, self._written - int(n))

    def read_page(self, limit: int = 2000, *, count: int = 0, mark: str = "",
                  older: bool = False, dev_ids=()) -> LogPage:
        """記録を、新しい方から読む範囲を決めて、古い順(表示の並び)で返す。

        count が 0 なら、新しい方から limit 行(1 頁)を読む。count は、前に読んだ
        行の数(前に返した LogPage の count)である。mark にそのときの印を渡すと、
        その後に書いた行を足して読む。新しい行が来ても、読み込んだ範囲の古い端は
        動かない(画面で古いログを読んでいる人の行が、上から消えない)。mark が
        なければ(または起動し直す前の印なら)、新しい方から count 行を読む(古い端は
        新しい行の分だけ進む)。ただし 1 頁より少なくはしない。記録が少なかったとき
        (削除した直後など)の行数のままにすると、その後に足された行が出ない。

        older は画面の「以前のログを表示」で、範囲を 1 頁(limit 行)古い方へ
        広げる。dev_ids(装置 ID の並び)を渡すと、その装置の行が増えるまで(または
        もう古い行がないところまで)1 頁ずつ広げる。1 頁は装置によらない行数なので、
        ほかの装置の行ばかりの頁を読むと、絞り込んだ画面には何も足されない。

        記録は書き込み中のファイルと古い世代に分かれているので、新しいファイル
        から古い世代へ、足りるまでたどる。行で数えるので、世代を分けても(書き
        込み中のファイルの名前が変わっても)範囲は変わらない。印は読むより先に
        取る。読む間に書かれた行は次に読むときにもう一度数えるので、範囲は古い方へ
        広がることはあっても、狭まらない。
        """
        stamp = self.mark()
        limit = max(1, limit)
        added = self._added_since(mark)
        want = (limit if count <= 0 else max(count, limit) if added is None
                else count + added)
        files = [*self.generations(), self.path()]
        lines: list[str] = []
        left = len(files)           # まだ読んでいないファイルの数(古い方から)

        def fill(n: int) -> None:
            """読んだ行が n 行以上になるか、ファイルが尽きるまで古い方へ読む。"""
            nonlocal lines, left
            while len(lines) < n and left > 0:
                left -= 1
                try:
                    lines = (files[left].read_text(encoding="utf-8").splitlines()
                             + lines)
                except OSError:
                    continue

        fill(want)
        if older:
            have = min(want, len(lines))
            while True:
                want = have + limit
                fill(want)
                got = min(want, len(lines))
                added = lines[len(lines) - got:len(lines) - have]
                if (not dev_ids or got == have
                        or any(_log_dev(x) in dev_ids for x in added)):
                    break
                have = got
        n = min(want, len(lines))
        out = [x for x in map(_parse, lines[len(lines) - n:]) if x is not None]
        return LogPage(out, len(lines) > want or left > 0, n, stamp)

    def read_view(self, limit: int = 2000, *, count: int = 0, mark: str = "",
                  older: bool = False, dev_ids=(), view: str = "") -> dict:
        """画面のログの欄が読む形(/api/logs)。1 行 1 周にまとめて返す。

        view は前の応答の印である。続きを読めるとき(「以前のログを表示」ではなく、
        印がこの起動で前に返したもので、その後にログを消していないとき)は、その応答の
        後に書いた行だけを読み、変わったまとまりだけを返す(delta)。毎秒の取得が読む
        量は新しい行の数だけで、読み込み済みの行の数によらない。返すのは、新しい
        まとまりと丸ごと変わったまとまり(laps。id で置き換えるか、後ろに足す)、行を
        足しただけのまとまり(add)、消えたまとまり(drop)、足した行の数(added)である。

        続きを読めないときは、read_page と同じ範囲を読み、すべてのまとまりを返す。
        どちらも、次に読むための印(view)と、この先の行が変えうるまとまりの id(open)
        を付ける。画面は、自動スクロールの間に古い行を落とすとき、open のまとまりを
        残す(続きの応答がそれを指す)。印は 1 回だけ使える。同じ印で 2 度読むと(取得が
        重なったとき)、2 度目は範囲の全体を読む。
        """
        if view and not older:
            got = self._read_on(view, limit)
            if got is not None:
                return got
        page, cleared = self._read_exact(limit, count=count, mark=mark, older=older,
                                         dev_ids=dev_ids)
        fold = LapFold()
        made = fold.feed(page.entries)["made"]
        return {"laps": [LapFold.shown(g, True) for g in made], "more": page.more,
                "count": page.count, "mark": page.mark, "page": max(1, limit),
                "open": fold.open_ids(),
                "view": self._keep_view(page.mark, fold, cleared)}

    def _read_exact(self, limit, **kw) -> tuple[LogPage, int]:
        """read_page と同じ範囲を、印と中身が揃った形で読む。消した回数も返す。

        read_page は印を読むより先に取るので、読む間に書かれた行も中身に入りうる。
        範囲を読むだけならそれでよいが、続き(_read_on)は印の後の行を読むので、同じ行を
        2 度まとめてしまう。読み終えた後の印が同じなら、読む間に書かれた行はない
        (書き込みはロックの中で行い、印もロックの中で読む)。読む間に行を消していたら
        (消した回数が変わった)、消している途中のファイルを読んだかもしれない。どちらか
        が 2 度とも起きたら、書き込みを待たせて読む。
        """
        for _ in range(2):
            cleared = self._cleared
            page = self.read_page(limit, **kw)
            with self._log_write_lock:
                # ほかのプロセスが書いていたら、印の名前が替わり、読み直す
                self._note_others()
                if self.mark() == page.mark and self._cleared == cleared:
                    return page, cleared
        with self._log_write_lock:
            return self.read_page(limit, **kw), self._cleared

    def _read_on(self, view: str, limit: int) -> dict | None:
        """印 view の応答の後に書いた行だけを読み、まとめた途中の状態に足す。続きを
        読めなければ None。"""
        with self._views_lock:
            kept = self._views.pop(view, None)
        if kept is None:
            return None
        mark, fold, cleared = kept
        with self._log_write_lock:
            self._note_others()
            added = self._added_since(mark)
            if added is None or self._cleared != cleared:
                return None
            stamp = self.mark()
            lines = self._newest(added)
        got = fold.feed([x for x in map(_parse, lines) if x is not None])
        return {"delta": True, "added": added, "mark": stamp, "page": max(1, limit),
                "laps": [LapFold.shown(g, True)
                         for g in [*got["changed"], *got["made"]]],
                "add": [{"id": i, "entries": es} for i, es in got["grew"].items()],
                "drop": got["gone"], "open": fold.open_ids(),
                "view": self._keep_view(stamp, fold, cleared)}

    def _newest(self, n: int) -> list[str]:
        """記録の新しい方から n 行(古い順)。ファイルの後ろから読む(_tail)。"""
        out: list[str] = []
        for f in reversed([*self.generations(), self.path()]):
            if len(out) >= n:
                break
            try:
                out = _tail(f, n - len(out)) + out
            except OSError:
                continue
        return out

    def _keep_view(self, mark: str, fold: LapFold, cleared: int) -> str:
        """まとめた途中の状態を覚え、次に読むための印を返す。cleared は、その状態を
        まとめたときの消した回数である。"""
        view = secrets.token_hex(8)
        with self._views_lock:
            self._views[view] = (mark, fold, cleared)
            while len(self._views) > self.VIEWS_KEEP:
                self._views.pop(next(iter(self._views)))
        return view

    @staticmethod
    def group_by_lap(entries: list[dict]) -> list[dict]:
        """記録を 1 行 1 周にまとめる。

        返すのは [{"dev", "lap", "at", "entries"}] で、並びは古い順である。まとめ方は
        LapFold.feed、まとまりの中の並びは LapFold.shown にある。
        """
        return [LapFold.shown(g) for g in LapFold().feed(entries)["made"]]

    def clear(self, dev_ids=()) -> None:
        """ログを消す。dev_ids(装置 ID の並び)を指定すると、その装置の行だけを消す
        (1 台ぶんの記録だけ整理したいとき。画面の絞り込みと対応し、1 つの行の、
        いまの装置と前に割り当てていた装置を一緒に渡す)。

        どちらも古い世代まで消す。画面の「ログを削除」は、元に戻せないことを
        確認してから消す操作である。書き込み中のファイルだけを消すと、
        「以前のログを表示」で消したはずの行がまた出て、消えたつもりで残る。
        """
        # 追記と重なると、全消去では開いているファイルを消せず(Windows)、1 台ぶんの
        # 消去では読み -> 選別 -> 書き戻しの間の追記が消える。どちらも書き込みの
        # ロックの中で行う。消した行は、画面がまとめた途中の状態にも残っていて、印の
        # 後の行の数も合わなくなるので、消し終えたら消した回数を数える(どの欄も次は
        # 範囲の全体を読み直す。read_view)
        with self._log_write_lock:
            try:
                self._clear(dev_ids)
            finally:
                self._cleared += 1
                # 自分で消した後の大きさ(ほかのプロセスが書いたとは見ない)
                self._size = self._size_now()

    def _clear(self, dev_ids) -> None:
        """clear の中身(書き込みのロックを持っている側から呼ぶ)。"""
        if not dev_ids:
            # ほかのスレッドが記録を読んでいる瞬間は Windows が消去を拒否する
            # ので、少し待って消し直す(remove_file)
            remove_file(self.path())
            for f in self.generations():
                remove_file(f)
            self._dropped_seen = False
            self._since_trim = 0
            return
        for f in [*self.generations(), self.path()]:
            try:
                lines = f.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                continue
            kept = [line for line in lines if _log_dev(line) not in dev_ids]
            if len(kept) != len(lines):
                write_file(f, "\n".join(kept) + ("\n" if kept else ""))
