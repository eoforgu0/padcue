"""装置プール。操作画面のサーバーの接続を管理する(登録した装置ぶんのリンクを持つ)。

契約(docs/specs/coupling.md D3):
- 1 装置 = 1 接続 = 1 装置 lock。状態収集(バックグラウンド)も操作(実行・停止・
  選択・手動操作)も、同じ接続を同じ lock の下で使う。装置は制御の接続を 4 本まで
  受けるので、分けても壊れはしない。1 本にまとめるのは順番を決める
  ためである。収集と操作が別の接続だと、操作の応答より先に古い状態が返って、
  画面が「押す前」を映すことがある。操作権も接続に結び付くので、接続の数を増やす
  ほど再取得が増える
- lock の粒度は 1 回の call(要求を 1 つか、続けていくつか送る)。収集は要求ごとに
  call を分けるので、収集の合間に操作が必ず割り込める
- 収集は装置ごとに独立したスレッドで並列。結果はキャッシュに置き、
  /api/state はキャッシュの値をすぐに返す。1 台の無応答は、その装置の表示が
  「未検出」になるだけで、ほかの装置の表示・操作を一切妨げない(要件 R13)
- 装置の記録の回収はここに一本化する(coupling.md D5)。装置は、受け取ったと
  知らされた記録だけを消す。読み出す側が 2 つあると、同じ記録を logs.jsonl へ
  2 度書く
"""
from __future__ import annotations

import threading
import time
import traceback

from . import owner, proto, runlog
from .client import (
    DeviceClient,
    DeviceError,
    LogReader,
    NotSentError,
    connect_verified,
    failure_text,
)
from .project import ConfigError
from .words import error_text, sentences, unexpected_text


class _NotKept(Exception):
    """読んだ記録をファイルに書けなかった(装置には受け取りを知らせない)。"""


def _mono() -> float:
    """単調時計。「未検出」の判定をテストで進められるよう、関数にしてある。"""
    return time.monotonic()


def host_mac(a, b) -> str:
    """HOST_INFO の a/b(ペアリング引数の先頭 8 バイト)から本体の MAC を取る。

    引数の構造は `[0]=フェーズ [1..6]=本体 BT MAC(LE) [7..]=フェーズ依存`
    (docs/design/procon-protocol.md §7)。本体固有なのは [1..6] だけで、
    先頭のフェーズ番号(01=新規ペアリング / 04=登録済みの記録手渡し)と
    末尾のバイトはフェーズによって変わる。8 バイトまるごとをキーにすると、
    同じ本体でも登録の前後で別物として扱われ、付けた名前が引き継がれない。
    """
    raw = f"{int(a):08x}{int(b):08x}"
    return raw[2:14]        # [1..6] の 6 バイト = 12 桁 hex


def _target(devcfg: dict) -> tuple:
    """つなぐ先(接続先と装置 ID)。どれかが変われば、いまの接続は使えない。"""
    return devcfg.get("host"), devcfg.get("port"), devcfg.get("id")


def keep_only(client: DeviceClient, device_ids: set, keep: set) -> set:
    """これから実行する手順だけを装置に残し、ほかを消す。

    周回数と開始位置は手順の中身に入るので、指定が違えば別の id になる。装置に
    溜めると、手順の一覧の上限(64 件)を同じ手順の別の id で埋めてしまう。実行の
    たびに掃除すれば、装置に残るのは、これから実行する 1 件だけになる。転送は
    1 秒もかからないので、残しておく利点はない。

    消せなかったものは次の機会に持ち越す(装置は待機中しか削除を受け付けない)。
    通信に失敗したら、残りは試さない。失敗した接続は閉じてあるので、続けても
    同じ失敗をくり返すだけである。その後の要求の失敗は、呼び出し元がいつもどおり扱う。
    """
    for did in list(device_ids):
        if did in keep:
            continue
        try:
            client.delete(did)
            device_ids.discard(did)
        except DeviceError:
            pass
        except OSError:
            break
    return device_ids


def identify_found(host: str, port: int, device_id: str, client_cls=None) -> None:
    """見つかった装置(未登録)の LED を点滅させる。

    登録していない装置には収集の接続がないので、この 1 回のために接続し、送ったら
    閉じる。操作権は取らない。LED を光らせるだけで装置の動きを変えないので不要で、
    取ると、登録していない装置をこの padcue が占有し、別の PC やツールが使えなくなる。

    報告した装置 ID が、見つかったときの装置 ID と違えば送らない。IP は DHCP で
    変わるので、探してから押すまでの間に、別の装置がそこにいることがある。判別する
    ための操作で別の装置が光ると、混同のもとになる。
    """
    c = (client_cls or DeviceClient)(host, int(port), timeout=3.0)
    info = c.connect()
    try:
        if info.device_id != device_id:
            raise DeviceError(
                "DEVICE_MISMATCH",
                f"{host} には別の装置(装置 ID {info.device_id[-4:].upper()})が"
                "接続されているため、LED を点滅できません")
        c.identify()
    finally:
        c.close()


# 読み出す側が替わった直後に、既に書いた記録を探す範囲(新しい方からの行数)。受け取りを
# 知らせていない記録は、最後に受け取った 1 回の応答ぶん(48 件)までである
_RECENT_LOGS = 2000


class LogCursors:
    """装置ごとの、記録をどこまで受け取って書いたか(装置 ID で探す)。

    リンクは改名や登録の変更で作り直される。受け取った所をリンクに持つと、
    書いてから受け取りを知らせるまでの間に作り直されたとき、新しいリンクが同じ
    記録をもう一度書く。プールが 1 つ持ち、作り直したリンクが引き継ぐ。読む間は
    装置ごとのロックを持ち、同じ装置を 2 つのリンクが同時に読まない。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_id: dict[str, tuple[LogReader, threading.Lock]] = {}

    def get(self, device_id: str) -> tuple[LogReader, threading.Lock]:
        with self._lock:
            return self._by_id.setdefault(device_id,
                                          (LogReader(), threading.Lock()))


class DeviceNotes:
    """装置について覚えておく値(装置 ID で探す)。

    リンクは改名で作り直される。保存した USB の問い合わせの間隔と、USB の再接続を
    指示した時刻をリンクに持つと、作り直したときに消え、保存した間隔が画面から
    消えて保存できていないように見え、指示した切断がログで異常の色になる。プールが
    1 つ持ち、作り直したリンクが引き継ぐ(LogCursors と同じ)。割り当てのない行
    (装置 ID が空)は、どの装置のことも覚えない。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_id: dict[str, dict] = {}

    def get(self, device_id: str, key: str, default=None):
        with self._lock:
            return self._by_id.get(device_id, {}).get(key, default)

    def put(self, device_id: str, key: str, value) -> None:
        if not device_id:
            return
        with self._lock:
            self._by_id.setdefault(device_id, {})[key] = value


def unwritten(project, dev: str, entries: list) -> list:
    """装置の記録のうち、logs.jsonl にまだ書いていないもの。

    同じ記録は、起動の boot と起動からの番号 seq が同じ行である
    (client.DeviceClient.logs が付ける)。受け取りを知らせるのは書いた後の次の
    LOGS なので、その間に padcue が終わると、次に起動した padcue は同じ記録を
    もう一度受け取る。読み出す側が始めて読むとき(受け取った所をまだ持たないとき)に
    使う。
    """
    keys = {(e.get("boot"), e.get("seq"))
            for e in project.logs.read(_RECENT_LOGS)
            if e.get("dev") == dev and "seq" in e}
    if not keys:
        return entries
    return [e for e in entries if (e.get("boot"), e.get("seq")) not in keys]


def is_busy(status: dict | None) -> bool:
    """装置が実行中か選択待ちか。状態の名前と、実行エンジンのフラグのどちらかで見る。

    画面(core.js の deviceBusy)も同じ規則で、開始のボタンを抑える。
    """
    st = status or {}
    return bool(st.get("running") or st.get("awaiting")
                or st.get("state") in ("RUNNING", "AWAITING"))


# USB の再接続を指示してから、その切断の記録を受け取るまでの上限(秒)
RECONNECT_NOTE_S = 15.0

# 装置の記録の状態の番号のうち、待ち(STATE の b。proto.STATE_AWAITING)
_AWAITING_STATE = proto.STATE_AWAITING

# 同じ起動の記録の t_ms がこれ(ミリ秒)より大きく戻ったら、装置の時計が一周したと
# 見る(_stamp_unread)。同じ起動の時計は戻らないので、戻るのは一周したときだけである。
# 一周すると、実機では約 49.7 日ぶん戻る
_CLOCK_WRAP_BACK_MS = 3_600_000


class LinkReading:
    """DeviceLink の、ある時点の状態とそこから出した時刻の写し(DeviceLink.reading)。

    画面の状態は、1 本のレーンの値(状態、終了予定、線の起点、図の選択)を、この
    写しから出す。写した値(状態とそれを読んだ時刻、実行の開始の時刻、最後に動かした
    手順、区切りとその時刻、経過フレーム数 0 の時刻、その実行で覚えた選択の手がかり)の
    ほかは、元の DeviceLink の今の値と操作を使う。
    """

    def __init__(self, link):
        self.link = link
        self.status = link.status
        self.status_at = link.status_at
        self.at = link.at
        self.run_started_at = link.run_started_at
        self.last_proc = link.last_proc
        self._mark_idx = link._mark_idx
        self.mark_at = link.mark_at
        self.frame0_at = link.frame0_at
        # その実行で覚えた選択の手がかり(どれも、変えるときは丸ごと差し替える)
        self.picked = link.picked
        self.picks_sent = link.picks_sent
        self.seen_states = link.seen_states
        self.drawn_picks = link.drawn_picks

    def __getattr__(self, name):
        return getattr(self.link, name)

    def picked_arm(self, status: dict) -> int | None:
        """写したときの、PC が送った選択で決まる選択肢の番号(DeviceLink.picked_arm と
        同じ決め方を、写した picked で)。"""
        return DeviceLink.picked_arm(self, status)

    def passed_at(self, mark: dict | None) -> float:
        """写したときに覚えていた区切りを、装置が通った時刻(DeviceLink.passed_at と
        同じ。違う区切りなら 0)。"""
        if not mark:
            return 0.0
        key = (mark.get("index"), tuple(mark.get("counters") or []))
        return self.mark_at if key == self._mark_idx else 0.0


class DeviceLink:
    """1 装置ぶんの接続・キャッシュ・収集スレッド。"""

    @property
    def registered_id(self) -> str:
        """登録情報の装置 ID。割り当てを解除した装置では空。

        つながる装置は、実機も模擬装置も必ず登録情報に id を持つ
        (client.connect_verified は id が空の装置にはつながない)。装置が
        報告した値(info)は使わない。割り当てを解除した後も、前の装置が
        報告した値が残っているからである。
        """
        return self.cfg.get("id") or ""

    @property
    def mismatch(self) -> bool:
        """登録した装置と違う装置が、その接続先にいるか。"""
        return (isinstance(self.error_exc, DeviceError)
                and self.error_exc.code == "DEVICE_MISMATCH")

    @property
    def binterval_saved(self) -> int | None:
        """保存した USB の問い合わせの間隔(なければ None)。

        装置は次の起動から使い、それまでは今の値を報告する。保存できたのに戻って
        見えないよう、ここで覚えておく(装置 ID ごと。DeviceNotes)。
        """
        return self._notes.get(self.registered_id, "binterval_saved")

    @binterval_saved.setter
    def binterval_saved(self, value: int | None) -> None:
        self._notes.put(self.registered_id, "binterval_saved", value)

    @property
    def reconnect_at(self) -> float:
        """画面から USB の再接続を指示した時刻(0 = 指示していない)。

        そのための切断を異常として示さない(装置 ID ごと。DeviceNotes)。
        """
        return self._notes.get(self.registered_id, "reconnect_at", 0.0)

    @reconnect_at.setter
    def reconnect_at(self, value: float) -> None:
        self._notes.put(self.registered_id, "reconnect_at", value)

    POLL_S = 1.0            # 収集周期(秒)。画面の表示が遅れて見える上限
    # 区切りの時刻を置き直す差の下限(秒)。これより小さい差は応答の遅れのばらつきとみなす
    MARK_RESYNC_S = 0.3
    # 「未検出」と判定するまでの無応答の長さ。装置側で操作権が
    # 失効する 30 秒より短くする
    GONE_S = 10.0

    def __init__(self, project, devcfg: dict, client_cls=None,
                 log_cursors: LogCursors | None = None,
                 notes: DeviceNotes | None = None):
        self.project = project
        # 装置について覚えておく値(プールと共有する。binterval_saved と
        # reconnect_at はここに置く)
        self._notes = notes or DeviceNotes()
        # 登録情報の 1 台ぶんのコピー(id・name・host・port のほか、video と
        # console_id も入る)
        self.cfg = dict(devcfg)
        self.client_cls = client_cls or DeviceClient
        self.lock = threading.Lock()     # 接続と client への唯一の経路
        self.client = None
        self._client_for: tuple | None = None   # client をつないだ先(_target)
        # サーバーを起動したときからある装置か。画面の最初の状態の取得は、この
        # 装置の最初の収集だけを待つ(DevicePool.refresh が付ける)
        self.from_start = False
        # ---- キャッシュ(collector が書き、/api/state が読む) ----
        # 状態のキャッシュの書き換えの順番。収集の「書き戻しより前に読んだか」の判定と
        # 代入を、操作の書き戻しと交互に行う
        self._cache_lock = threading.Lock()
        self.info = None                 # DeviceInfo(直近の HELLO)
        # 接続した回数。更新の後に装置が再起動したことを判別する
        self.connects = 0
        self.status: dict = {}           # 直近の STATUS
        # 直近の STATUS を読めた時刻(UNIX 秒。0 = まだ読めていない)。at は失敗した
        # 収集でも進むので、「最後に確認した時刻」には使えない
        self.status_at: float = 0.0
        self.listing: set = set()        # 直近 LIST の手順 id の集合
        # 応答がなくなった時刻(単調時計。None = 直近の収集は成功した)。
        # 書くのは error のセッターだけである
        self._err_since: float | None = None
        self._error: str = ""            # 直近の収集エラー(空 = 応答があった)
        self.error_exc: Exception | None = None   # 同・例外そのもの
        # 収集の失敗の、利用者に見せる理由(reason が読む。_fail だけが書く)
        self._reason: str = ""
        # 直近の想定外の例外。同じ理由の traceback を毎秒出さないための記録
        self._last_unexpected: Exception | None = None
        # 装置から読んだものを PC に保存できない理由(空 = 保存できた)。装置との
        # 通信は正常なので、収集の失敗(error)とは分ける
        self.save_error: str = ""
        # 装置の記録をどこまで受け取って書いたか(装置ごと。プールと共有する)
        self._log_cursors = log_cursors or LogCursors()
        self.console_id: str = ""         # つながっている本体の識別子(MAC 12 桁 hex)
        # 登録情報にまだ書けていない本体の識別子(空 = 書いた)
        self._console_id_unsaved: str = ""
        self.at: float = 0.0             # キャッシュ時刻(UNIX 秒)
        self.run_started_at: float = 0.0  # 実行中ならその開始時刻(0 = 実行なし)
        # 実行が停止した時刻(UNIX 秒。0 = まだ停止していないか、見積もれない)。停止した
        # のを読むのは収集の周期ごとなので、最後に実行中と読んだ時刻と、そこから停止する
        # までに進んだ経過フレーム数から出す(_note_run)。記録の終わりの行の時刻になる
        self.run_ended_at: float = 0.0
        # 停止した時刻(run_ended_at)のうち、停止したときの装置の行にまだ使っていない
        # もの(0 = ない)。その行を受け取ったら、この時刻で書く(_stamp_stop)
        self._stop_stamp: float = 0.0
        # 最後に実行中と読んだ (時刻, 経過フレーム数, 選択待ちの上だったか)
        self._ran: tuple[float, int, bool] | None = None
        # 最後に読めた実行の識別(proto.run_of)。新しい実行の見分け(_note_run)に使う
        self._run_id: tuple[int, int] | None = None
        # 最後に動かした手順の id。STATUS の proc は手順のバッファーにある手順の id で、
        # 実行が終わった後に別の手順を送ると、その手順に替わる。停止した後に
        # 「停止した位置」を出すには、どの成果物で実行していたかが必要なので、
        # 実行中に見た proc を覚える
        self.last_proc: str = ""
        # 最後に停止した実行の (手順の id, 停止したと読んだ状態, 停止した時刻)。次の
        # 実行が始まっても、次に停止するまで残す。操作画面のサーバーを通らずに次の実行を
        # 始められると、前の実行の止まり方と停止した所は装置から読めなくなるので、
        # 監視がその実行の終わりをまだ書いていなければ、これで書く
        # (Coupler._end_run_taken_over)
        self.ended_run: tuple[str, dict, float] | None = None
        # 最後に通った区切りを通った時刻(UNIX 秒。0 = まだ通っていない)。
        # 図の上の現在位置は、この区切りからの経過で出す
        self.mark_at: float = 0.0
        self._mark_idx: tuple | None = None
        self._mark_frame: float | None = None   # その区切りを通った時点の経過フレーム数
        # 実行の経過フレーム数 0 に当たる時刻(UNIX 秒。0 = まだ分からない)。装置の
        # 時計から出す開始の時刻で、区切りをまだ通っていない間の線と、終わる時刻の
        # 起点である(_note_start)
        self.frame0_at: float = 0.0
        # PC が最後に送った選択待ちの選択(装置の await_gen, 選択肢の番号)。装置は
        # 選択待ちを抜けた後、どの選択肢へ進んだかを返さない。その直後の止まる先を、
        # その選択肢でたどるのに使う(picked_arm)
        self.picked: tuple[int, int] | None = None
        # この実行で PC が送った選択。(送った中身の id, 選択待ちのレコード番号,
        # そのときのカウンターの値, 選択肢の番号) の並び(note_pick)。レーンの図を、
        # 選んだ選択肢で描くのに使う(positions.picks_now)。監視がレーンの記録に残す
        # までの間の分で、新しい方から PICKS_SENT_MAX 件だけ持つ
        self.picks_sent: list = []
        # この実行で読めた装置の状態のうち、どの選択肢を通ったかの手がかりになる欄
        # (送った中身の id、最後に出したレコード、最後に通った区切り)。装置は選択肢を
        # 出た後、どの選択肢を通ったかを返さないので、次に図を作るまでの間に選択肢を
        # 出ても、図に入れられるように持つ(positions.picks_now の seen)。変わった
        # ときだけ足し、新しい方から SEEN_STATES_MAX 件だけ持つ(_note_seen)
        self.seen_states: list = []
        # 画面が最後に描いた図の、選んだ選択(送った中身の id, その実行の開始時刻
        # run_started_at, pick_of の形の並び)。次に図を作るときに、同じ実行の前に
        # 分かった選択として使う(guistate の _figure)。
        # 監視がレーンの記録に残さない実行(画面を通さずに始めた実行)でも、古い状態が
        # seen_states から落ちた後まで、その周に選んだ選択肢を描き続けられる
        self.drawn_picks: tuple = ("", 0.0, [])
        self._wt_at: float = 0.0         # 書き戻し(write-through)の時刻(単調時計)
        # 最後に未検出から戻ったときの装置の状態(STATUS の state)
        self.state_at_back: str = ""
        # 未検出から戻った回数と、最後に応答がなくなる前に読めた STATUS。監視は自分の
        # 周期(0.5 秒)ごとに未検出かを読むので、未検出になってすぐ戻ると、一度も
        # 未検出と読まないことがある。戻った回数が増えたことで、未検出になって戻った
        # ことを知る(Coupler._note_gone)。戻った後の status は戻った後に読んだ値なので、
        # 未検出になる前の状態は、応答がなくなったときに覚えておく
        self.back_count: int = 0
        self.status_at_loss: dict = {}
        # 最後に応答がなくなった時刻(UNIX 秒。0 = まだない)。応答がなくなり始めた
        # 収集を始めた時刻で、最後に読めた時刻より後である。未検出になった時の行の
        # 時刻にする(Coupler._note_lost)。監視が気づくのは 10 秒後か、短い未検出では
        # 戻った後なので、気づいた時刻で書くと、未検出の間に起きた行(装置の起動など)
        # が前に並ぶ
        self.lost_at: float = 0.0
        self._tried_at: float = 0.0       # いまの収集を始めた時刻(UNIX 秒)
        # 収集が読めなかった間があり、その後の記録をまだ受け取っていないか。受け取った
        # 記録のうち、その間に装置が書いた行は、装置の時計から出した時刻で書く
        # (_stamp_unread)
        self._missed = False
        # 装置の時計(記録の t_ms。起動からのミリ秒)を PC の時計に換算するための差
        # (起動の印 boot, 秒)。行を書いた PC の時刻 = t_ms / 1000 + 差。差は分から
        # ないので、確かに言える上限を持つ(_stamp_unread)
        self._clock: tuple[int, float] | None = None
        # _clock の起動で最後に受け取った行の t_ms。t_ms が大きく戻ったら、装置の時計が
        # 一周した(_stamp_unread)
        self._clock_t: float | None = None
        # いまの実行を初めて実行中と読んだ時刻(UNIX 秒)と、それをまだ実行の開始の行に
        # 使っていないか(_stamp_unread)。run_started_at は周回を終えるための続きで元の
        # 実行の開始の時刻に置き替わる(carry_run_start)ので、別に持つ
        self._began_at: float = 0.0
        self._began_open = False
        # 装置の記録を最後に読み切った読み取りを始めた時刻(UNIX 秒。0 = まだない)。
        # その後に受け取る行は、どれもこれより後に書かれている(_stamp_unread)
        self._drained_at: float = 0.0
        self._stop = False
        self._thread: threading.Thread | None = None

    # ---- 「見えている」と「未検出」 ----
    #
    # どちらもここ 1 か所で決める。画面(/api/state)も監視(coupler)も通知も、
    # 同じ値を読む。2 つは別の値である。応答はあるのに USB が外れている装置は、
    # 「未検出」ではないが「見えている」でもない。

    @property
    def error(self) -> str:
        """直近の収集エラー(空 = 応答があった)。"""
        return self._error

    @error.setter
    def error(self, msg: str) -> None:
        # 応答がなくなった時刻を記録するのはこの代入だけである。収集の成否は
        # すべてここを通るので、時刻を別に数え直す場所を作らない
        self._error = msg or ""
        if not self._error:
            if self.gone:
                # 未検出から戻った。そのとき装置がどの状態だったか(この収集で
                # 読んだ値)を覚える。監視が戻ったことを記録するのは自分の周期の
                # ときで、それまでに利用者が停止すると、状態は停止の後の値に
                # 書き戻されている
                self.state_at_back = self.status.get("state") or ""
                # 戻ったときの状態を置いてから数える(数が増えたのを見た監視が、
                # 前に戻ったときの状態を読まない)
                self.back_count += 1
            self._err_since = None
        elif self._err_since is None:
            self._err_since = _mono()
            self.status_at_loss = dict(self.status)
            self.lost_at = (self._tried_at if self._tried_at > self.status_at
                            else time.time())
            self._missed = True

    @property
    def reason(self) -> str:
        """収集の失敗の、利用者に見せる理由(空 = 応答があった)。

        装置の行と開始の拒否は、どちらもこれを読む。error は失敗しているかの
        判定と記録のためのもので、OS の英語の文や符号を含む。
        """
        return (self._reason or self._error) if self._error else ""

    def _ok(self) -> None:
        """収集が成功した。"""
        self.error = ""
        self.error_exc = None
        self._reason = ""

    def _fail(self, e: Exception, why: str) -> None:
        """収集が失敗した。why は利用者に見せる理由である。

        理由を先に置く。画面の要求のスレッドが、失敗だけが入って理由がまだない
        瞬間に読むと、OS の英語の文が出る。
        """
        self._reason = why
        self.error_exc = e
        self.error = str(e) or type(e).__name__
        # 保存できないことは、読めた周の話である。つながらない間に残すと、
        # 装置の行が、つながらない理由の代わりに古い保存の失敗を示す
        self.save_error = ""

    @property
    def gone(self) -> bool:
        """「未検出」。連続して GONE_S 秒間応答がない状態。

        数え始めるのは、最初に失敗した収集が終わった時点である。接続の待ちが
        3 秒あるので、装置が応答しなくなってから判定までは、その分だけ長くなる。
        """
        since = self._err_since   # 収集スレッドが消しうるので、読むのは 1 回
        return since is not None and _mono() - since >= self.GONE_S

    @property
    def visible(self) -> bool:
        """「見えている」。応答があり、かつ Switch と USB でつながっている。"""
        return not self._error and bool(self.status.get("usb_mounted"))

    @property
    def owned_by_other(self) -> bool:
        """操作権を別の padcue が持っている。

        所有者がいないことは「別の padcue が持っている」ではない。取れていない
        だけなので、収集が次の周期で取得し直す。ここを真にすると、その 1 周期
        だけ「操作権なし」のバッジが出て、押しても何も起きないボタンになる。
        """
        own = self.status.get("owner") or None
        return bool(own and own.get("id") != owner.client_id())

    # ---- 接続(必ず lock の下で) ----

    def _ensure(self):
        # 閉じたリンク(登録から外した装置)へは、つなぎ直さない。収集は close の
        # 後も 1 周の続きを行うので、つなぎ直すと操作権を取り、装置の記録を
        # 読み出して消してしまう
        if self._stop:
            raise DeviceError("CLOSED", f"装置「{self.cfg.get('name', '')}」は"
                                        "登録されていません")
        # 登録情報のコピーは、lock と関係なく差し替わる(DevicePool.refresh)。
        # 読むのは 1 回にする。いまの接続が別の接続先か別の装置 ID へ向けて
        # つないだものなら、使わずにつなぎ直す
        cfg = self.cfg
        if (self.client is not None and self._client_for == _target(cfg)
                and self.client.is_alive()):
            return self.client
        self._drop()
        if not cfg.get("host"):
            raise DeviceError("NO_HOST", "接続先が未設定です")
        c, info = connect_verified(cfg, timeout=3.0,
                                   client_cls=self.client_cls)
        self._client_for = _target(cfg)
        self.info = info
        self.connects += 1
        # つないだら操作権を取得。待機中も持ち続けるのは、別のツールが待機中の
        # 装置へうっかり実行を送る事故を防ぐためである。ほかの padcue が
        # 持っていれば取れないが、停止と状態の読み出しはそれでもできる。
        # 拒否されたときだけ続ける。通信の失敗は上へ伝え、この接続は使わない
        try:
            c.acquire()
        except DeviceError:
            pass
        except BaseException:
            # まだ self.client に入れていないので、ここで閉じないと残る
            c.close()
            raise
        self.client = c
        return c

    def _drop(self) -> None:
        c, self.client = self.client, None
        self._client_for = None
        if c is not None:
            try:
                c.close()
            except OSError:
                pass

    def _note_console_id(self, mac: str) -> None:
        """つながっている本体を覚える。登録情報へは _save_console_id が書く。"""
        if not mac or mac == self.console_id:
            return
        self.console_id = mac
        self._console_id_unsaved = mac

    def _save_console_id(self) -> None:
        """覚えた本体の識別子を登録情報に書く。書けなければ例外を上げ、次の収集で
        書き直す。

        登録情報のコピー(self.cfg)は書き換えない。コピーを差し替えるのは
        DevicePool.refresh だけで、ここで古いコピーから作り直すと、同じ時に
        refresh が反映した割り当ての解除や接続先の変更を消しうる。登録情報の
        ファイルに書いた値は、次の refresh がコピーに入れる。
        """
        mac = self._console_id_unsaved
        cfg = self.project.load_config()
        for i, d in enumerate(cfg.get("devices", [])):
            if d.get("name") == self.cfg.get("name"):
                self.project.update_device(cfg, i, console_id=mac)
                break
        if self._console_id_unsaved == mac:
            self._console_id_unsaved = ""

    # ---- 操作(収集と同じ接続・同じ lock を通る。装置への操作はすべてここを通す) ----

    def call(self, fn):
        """装置への 1 つの操作。fn は要求を 1 つか、続けていくつか送る。

        やり直すのは、要求を送信する前に切れていたと分かるときだけ
        (NotSentError)。装置が受け取ってしまった可能性がある切れ方で
        やり直すと、実行や転送が二重に行われる。RUN を送り直せば BUSY で拒否され、
        呼び出し元は「拒否された」と受け取って、実行している装置を監視外に
        置いてしまう。

        TimeoutError も同じ理由でやり直さない。

        操作権が失効していたときだけは、取得し直して送り直す。
        装置は操作権を確認してから中身を見るので、拒否された要求は実行されて
        いない。PC がスリープから復帰した直後がこの場面である。
        """
        with self.lock:
            c = None
            try:
                c = self._ensure()
                done = c.replies
                return fn(c)
            except NotSentError:
                self._drop()
                # fn は要求をいくつか続けて送ることがある(RUN の後の STATUS など)。
                # 送り直してよいのは、fn の最初の要求が送られなかったときだけである。
                # 応答まで済んだ要求があれば、送り直すとそれをもう一度送る
                if c is not None and c.replies != done:
                    raise
                return fn(self._ensure())   # 装置は何も受け取っていない
            except DeviceError as e:
                if e.code != "NOT_OWNER":
                    raise                # プロトコル上の拒否。接続は正常
                # 取得し直せなければ(別の padcue が持っている)、元のエラーを返す。
                # 奪い取るのは利用者が押したときだけである
                c = self._ensure()
                try:
                    c.acquire()
                except DeviceError:
                    raise e from None
                return fn(c)
            except OSError:
                self._drop()
                raise

    def write_through(self, status: dict | None = None,
                      listing: set | None = None) -> None:
        """操作の直後に、その場で分かった最新値をキャッシュへ書き戻す。

        次の収集(最大 1 秒後)を待つと、転送直後の listing や停止直後の
        「実行中」表示が古いまま残る。時刻印を付け、操作より前にフェッチを
        始めていた収集がこの新しい値を古い結果で上書きしないようにする。
        """
        with self._cache_lock:
            self._wt_at = time.monotonic()
            if status is not None:
                self.status = status
                self.status_at = time.time()
                self._note_run(status)
            if listing is not None:
                self.listing = listing

    def _note_run(self, status: dict) -> None:
        """実行の様子を観測する(開始時刻・待ち・最後に通った区切り)。

        開始時刻(run_started_at)は画面の「開始」に出す。装置は「いつ始めたか」を
        持たず、経過フレーム数だけを返す。選択待ちの間はフレームが進まないので、
        経過から逆算した開始時刻は待った分だけ後ろへずれていく。そこで実行中に
        なった瞬間を記録する(操作の書き戻しではすぐに、外(CLI)から始めた実行には
        収集の周期のぶん、最大 1 秒遅れて気づく。秒単位の表示には足りる)。終わる時刻の
        起点は、経過フレーム数から出す開始の時刻(frame0_at。_note_start)である。
        """
        on = bool(status.get("running") or status.get("awaiting"))
        # 停止を一度も読まないまま次の実行が始まっていたら(ほかの道具が、収集の間に
        # 終えて始め直した)、新しい実行として扱う。装置が返す実行の識別(起動の印と
        # 実行の通し番号)で見分ける。見分けないと、前の実行の開始時刻と、選んだ
        # 選択肢(図に描く)を持ち越す。同じ起動で番号が前に読んだより小さい状態は、
        # 古い状態である(操作の書き戻しは、収集が置いたより古い状態を置くことがある)。
        # 番号を戻さず、新しい実行とも見なさない
        rid = proto.run_of(status) if on else None
        known = self._run_id
        newer = bool(rid and (known is None or rid[0] != known[0] or rid[1] > known[1]))
        again = bool(on and self.run_started_at and known and newer)
        if newer:
            self._run_id = rid
        if on and status.get("proc"):
            self.last_proc = status["proc"]
        if not on:
            self.run_started_at = 0.0
            self._began_open = False
        elif again or not self.run_started_at:
            self.run_started_at = time.time()
            self._began_at, self._began_open = self.run_started_at, True
            # 区切りの時刻も持ち越さない。経過フレーム数は実行ごとに 0 から
            # 数え直すので、前の実行の区切りからは導けない
            self.mark_at, self._mark_idx, self._mark_frame = 0.0, None, None
            self.frame0_at = 0.0
            self.picked = None
            self.picks_sent = []
            self.seen_states = []
            self.drawn_picks = ("", 0.0, [])
            self.run_ended_at = 0.0
            self._stop_stamp = 0.0
        self._note_end(status, on)
        self._note_start(status, on)
        self._note_mark(status)
        if on:
            self._note_seen(status)

    def _note_seen(self, status: dict) -> None:
        """実行中の状態のうち、どの選択肢を通ったかの手がかりになる欄を覚える
        (seen_states)。

        直前に覚えたものと同じなら足さない。どの選択肢かは、図を作るときに、送った中身の
        レコードの範囲と比べて決める(positions.seen_picks)。
        """
        lm = status.get("last_mark") or {}
        item = {"proc": status.get("proc") or "",
                "event_index": status.get("event_index"),
                "last_mark": {"index": lm.get("index"),
                              "counters": list(lm.get("counters") or [])}}
        if self.seen_states and self.seen_states[-1] == item:
            return
        self.seen_states = [*self.seen_states, item][-self.SEEN_STATES_MAX:]

    def _note_start(self, status: dict, on: bool) -> None:
        """実行の経過フレーム数 0 に当たる時刻(frame0_at)を、装置の時計から覚える。

        読んだ時刻から経過フレーム数の長さを引く。そのまま使うと、応答が返るまでの
        時間のばらつき(無線の実機で数十〜百数十ミリ秒)だけ、読むたびに違う値になる。
        区切りを通った時刻(_note_mark)と同じく、差が小さいうちは動かさない。選択待ちの
        間はフレームが進まないので置き直さない(待ちが終わった後に、待った長さだけ後ろへ
        置き直す)。
        """
        fe = status.get("frames_elapsed")
        if not on or not isinstance(fe, int):
            return
        if status.get("awaiting") and self.frame0_at:
            return
        period_ns = (self.info.frame_period_ns if self.info
                     else proto.DEFAULT_FRAME_PERIOD_NS)
        at = time.time() - fe * period_ns / 1e9
        if self.frame0_at and abs(at - self.frame0_at) < self.MARK_RESYNC_S:
            return
        self.frame0_at = at

    def _note_end(self, status: dict, on: bool) -> None:
        """実行が停止した時刻を見積もる(run_ended_at)。

        停止したのを初めて読んだときに、最後に実行中と読んだ時刻へ、そこから進んだ経過
        フレーム数のぶんを足す。その間に選択待ちに入っていたら、待った長さは数えないので
        早めに出る。毎秒読めていれば、早まるのは読む周期の 1 秒より短い。読めなかった間
        (未検出の間など)をはさむと、その間に通った待ち(時間切れで先へ進んだ選択待ちなど)
        の長さだけ早く出る。読んだ時刻より後にはしない(経過フレーム数を実時間より速く
        進める模擬装置では、足したぶんが実時間より長い)。

        選択待ちの上で停止したとき(最後に選択待ちと読み、そこからフレームが進んで
        いない)は、停止したのを読んだ時刻にする。待つ間はフレームが進まないので、足す
        ぶんがなく、最後に読んだ時刻のままでは、停止の操作より前の時刻になる。画面から
        停止したときは、停止した直後に状態を読むので、読んだ時刻がほぼ停止した時刻で
        ある。
        """
        fe = status.get("frames_elapsed")
        if on:
            if isinstance(fe, int):
                self._ran = (time.time(), fe, bool(status.get("awaiting")))
            return
        if self._ran is None:
            return
        at, before, waiting = self._ran
        self._ran = None
        if isinstance(fe, int):
            period_ns = (self.info.frame_period_ns if self.info
                         else proto.DEFAULT_FRAME_PERIOD_NS)
            moved = max(0, fe - before)
            now = time.time()
            self.run_ended_at = (now if waiting and not moved
                                 else min(now, at + moved * period_ns / 1e9))
            self._stop_stamp = self.run_ended_at
        self.ended_run = (self.last_proc, dict(status),
                          self.run_ended_at or time.time())

    def _note_mark(self, status: dict) -> None:
        """最後に通った区切りを、いつ通ったかを覚える。

        図の上の現在位置は、この区切りからの経過で出す。装置が返す経過
        フレーム数は、開始位置から始めると 0 から数え直しになるので使えない。
        """
        # 回数も見る。周回数のくり返しは同じレコードへ戻ってくるので、番号だけ
        # だと周が変わったことに気づけず、線が周をまたいでずれていく
        lm = status["last_mark"]
        now = None if lm is None else (lm["index"], tuple(lm["counters"]))
        changed = now != self._mark_idx
        self._mark_idx = now
        if now is None:
            self.mark_at = 0.0
            return
        # 通った時刻は、装置の時計から出す。状態を読んだ時刻をそのまま使うと、
        # 読む周期(1 秒)のぶんだけ遅れた時刻になり、線が区切りのたびに
        # 最大 1 秒戻って見える。装置は区切りを通った時点のフレーム数
        # (last_mark.frame)と、いまの経過フレーム数(frames_elapsed)を同じ
        # 応答で返すので、その差が「通ってからの経過」である。装置は経過を
        # 区切りより先に読むので、区切りを通った直後の応答では、経過が区切りに
        # 達していないことがある。そのときは今通ったものとする。
        #
        # 区切りが変わらない間も、読むたびに見直す。選択待ちの間はフレームが
        # 進まないので、待ちが終わった後は「通ってからの経過」が待った時間を含ま
        # ない。見直さないと、線は待った時間のぶん先へずれる。選択待ちの間は
        # 見直さない。着いた時刻が待った長さの起点だからである。
        # ただし、差が小さいうちは動かさない。応答が返るまでの時間は読むたびに
        # ばらつく(無線の実機で数十〜百数十ミリ秒)ので、そのまま置き直すと線が
        # 毎秒わずかに前後する。区切りが変わったときも同じで、前の区切りの時刻
        # からフレーム数で導いた時刻が測った時刻と大きく違わなければ、導いた
        # 時刻を使う。装置のフレーム数は正確で、測った時刻にはばらつきがある
        frame = lm["frame"]
        if not changed and status.get("awaiting"):
            return
        # 1 フレームの長さは HELLO で知る。最初の収集の前に操作の
        # 書き戻しが来たときだけ、まだ知らないので 60 フレーム毎秒とみなす
        period_ns = (self.info.frame_period_ns if self.info
                     else proto.DEFAULT_FRAME_PERIOD_NS)
        ago = max(0, status["frames_elapsed"] - frame)
        at = time.time() - ago * period_ns / 1e9
        prev_at, prev_frame = self.mark_at, self._mark_frame
        if not changed and abs(at - prev_at) < self.MARK_RESYNC_S:
            return
        if not changed and not (status.get("running") or status.get("awaiting")):
            return           # 停止している間は動かない(次の実行の始まりで捨てる)
        if (changed and prev_at and prev_frame is not None
                and frame >= prev_frame):
            derived = prev_at + (frame - prev_frame) * period_ns / 1e9
            if abs(at - derived) < self.MARK_RESYNC_S:
                at = derived
        self.mark_at = at
        self._mark_frame = frame

    def note_pick(self, gen, arm: int, status: dict | None = None) -> None:
        """選択待ち(世代 gen)へ選択肢 arm の選択を送った(装置が受けた)ことを覚える。

        選択を送る所(画面、コマンド、条件の判定、スクリプトの後)が、装置が受けた直後、
        状態を書き戻す前に呼ぶ。後に呼ぶと、その間に作った画面の状態は、どの選択肢へ
        進んだかを知らない状態になる。status は、送る前に読んだ、その選択待ちで
        止まっている状態である(渡さなければ、最後に収集した状態)。
        """
        if not isinstance(gen, int):
            return
        self.picked = (gen, int(arm))
        # どの選択待ちで選んだかは、選ぶ前に読んだ状態から特定する(選んだ後の状態は、
        # 止まっていた選択待ちを示さない)。その状態が別の選択待ちのものなら残さない
        # (装置の状態から特定できたときに、図に入る。positions.seen_picks)
        st = status or self.status or {}
        lm = st.get("last_mark") or {}
        if (st.get("awaiting") and st.get("await_gen") == gen
                and isinstance(st.get("event_index"), int)):
            self.picks_sent = [*self.picks_sent, (
                st.get("proc") or "", st["event_index"],
                list(lm.get("counters") or []), int(arm))][-self.PICKS_SENT_MAX:]

    PICKS_SENT_MAX = 64

    def reading(self) -> LinkReading:
        """今の状態と、その状態から出した時刻(実行の開始時刻、最後に通った区切りと
        それを通った時刻、経過フレーム数 0 の時刻)を、収集と同じ排他の中で一緒に写す
        (LinkReading)。別々に読むと、その間に収集が次の状態を覚えたとき、前の状態と
        次の状態の時刻の組になる(区切りを通った時刻が求められず、終了予定が消える。
        前の実行の状態と新しい実行の開始時刻の組になる)。"""
        with self._cache_lock:
            return LinkReading(self)

    def carry_run_start(self, at: float) -> None:
        """実行の開始の時刻を at にする(周回を終えるための続きを、元の実行と 1 回の
        実行に見せる。Coupler._record_group)。もう停止したと読んでいれば変えない
        (停止している装置に開始の時刻を残さない)。"""
        with self._cache_lock:
            if self.run_started_at:
                self.run_started_at = at

    def keep_drawn(self, proc: str, run: float, picks: list) -> None:
        """画面が描いた図の選択を drawn_picks に残す。run は、図を作った状態と一緒に
        読んだ開始時刻(reading)で、その実行がまだ続いているときだけ残す。"""
        with self._cache_lock:
            if run and run == self.run_started_at:
                self.drawn_picks = (proc, run, picks)
    # 状態は読むたびに変わるので、収集(1 秒ごと)と書き戻しで 1 分ほどの分になる。
    # 図を作る間隔(画面の取得、監視の 0.5 秒)より十分に長い
    SEEN_STATES_MAX = 64

    def picked_arm(self, status: dict) -> int | None:
        """装置が状態 status の時点で最後に抜けた選択待ちで、PC が送った選択肢の番号。
        その選択待ち(世代)へ PC が選択を送っていなければ None。

        装置の await_gen は、次の選択待ちに入るまで、最後に入った選択待ちの世代のままで
        ある。選択待ちの間はまだ抜けていないので None を返す。
        """
        got = self.picked
        if got is None or status.get("awaiting") \
                or status.get("await_gen") != got[0]:
            return None
        return got[1]

    def passed_at(self, mark: dict | None) -> float:
        """その区切り(STATUS の last_mark)を装置が通った時刻(UNIX 秒)。いま覚えて
        いる区切りと違えば 0 を返す。

        時刻は、収集が経過フレーム数から出したもの(_note_mark)である。監視は状態を
        読んだ後でこれを調べるので、その間に収集が次の区切りを覚えていたら、読んだ状態の
        区切りの時刻ではない。区切りと回数で比べて、違えば使わせない。
        """
        if not mark:
            return 0.0
        key = (mark.get("index"), tuple(mark.get("counters") or []))
        with self._cache_lock:
            return self.mark_at if key == self._mark_idx else 0.0

    # ---- 収集 ----

    def start(self) -> None:
        if self._thread is not None:
            return
        # つながっている本体は登録情報に持つ。本体の識別子(HOST_INFO)は USB を
        # 挿した瞬間にしか流れてこないので、覚えていないと、起動し直すたびに
        # 「次の抜き挿しまで不明」に戻る。記録から読み直す手もあるが、記録は
        # 有限なので、朝に挿して夕方に画面を起動し直すと読めなくなる
        self.console_id = self.cfg.get("console_id", "") or ""
        self._thread = threading.Thread(
            target=self._collect_loop, daemon=True,
            name=f"collect-{self.cfg.get('name', '?')}")
        self._thread.start()

    def stop(self) -> None:
        self._stop = True

    def close(self) -> None:
        """収集を終了し、接続を閉じる。lock は待たない。

        無応答の装置の収集は、接続の待ち(最大 3 秒)の間 lock を持つ。ここで
        待つと、画面の終了やテストの後始末がその分だけ遅れる。lock が空いていれば
        ここで閉じ、使われていれば、収集が終了するときに自分の lock の下で閉じる
        (_collect_loop の最後)。
        """
        self.stop()
        if self.lock.acquire(blocking=False):
            try:
                self._drop()
            finally:
                self.lock.release()

    def forget_device(self) -> None:
        """前の装置から得た値を捨てる(割り当てを解除したか、別の装置を割り当てた)。

        HELLO の結果(版など)、状態、手順の一覧、最後に動かした手順、区切りの
        時刻である。残すと、どの装置にもつながっていない行が前の装置の版で
        「ファームウェアを更新してください」と出続ける。保存した USB の問い合わせの
        間隔と再接続の時刻は装置 ID で探すので(DeviceNotes)、ここでは変えない。
        登録情報の行の値(名前、映像、つながっている本体)は残す。
        """
        with self._cache_lock:
            self.info = None
            self.status = {}
            self.status_at = 0.0
            self.status_at_loss = {}
            self._clock = None
            self._clock_t = None
            self.listing = set()
            self.last_proc = ""
            self.ended_run = None
            self.run_started_at = 0.0
            self.run_ended_at, self._ran = 0.0, None
            self._run_id = None
            self._stop_stamp = 0.0
            self.mark_at, self._mark_idx, self._mark_frame = 0.0, None, None
            self.frame0_at = 0.0
            self.picked = None
            self.picks_sent = []
            self.seen_states = []
            self.drawn_picks = ("", 0.0, [])

    def _collect_loop(self) -> None:
        while not self._stop:
            t0 = time.monotonic()
            if not self.cfg.get("id"):
                # 未割り当ての行(割り当てを解除した直後)。つなぎに行かず、失敗
                # にもしない。状態は行のラベルとレーンのバッジが出す
                with self.lock:
                    self._drop()
                self.forget_device()
                self.save_error = ""
                self._ok()
                self.at = time.time()
                self._wait(self.POLL_S)
                continue
            self._collect_once(t0)
            self.at = time.time()
            # 1 秒周期(処理時間ぶんは差し引く)。停止指示には早めに気づく
            self._wait(max(0.1, self.POLL_S - (time.monotonic() - t0)))
        with self.lock:
            self._drop()

    def _collect_once(self, t0: float) -> None:
        """収集の 1 周。t0 はこの周を始めた時刻(単調時計)である。"""
        self._tried_at = time.time()
        try:
            # lock は 1 コマンドずつ取得し直す(操作が合間に割り込めるように)
            info = self.call(lambda c: c.hello())
            status = self.call(lambda c: c.status())
            if (status.get("owner") or None) is None:
                # 所有者がいないなら取得し直す。つないだときにも取るが、
                # 別のツールが取って返した後や、操作権が無音で失効した後は
                # 空のままになる。取得し直さないと、待機中に見えているのに
                # 開始だけが拒否される
                try:
                    status = self.call(
                        lambda c: (c.acquire(), c.status())[1])
                except (DeviceError, OSError):
                    pass          # 取れなければ次の周期でまた試す
            listing = {e["id"] for e in self.call(lambda c: c.list())}
            with self._cache_lock:
                if self._wt_at < t0:
                    # フェッチを始めた後に操作の書き戻しが入っていたら、
                    # こちらの(古い)結果でそれを上書きしない
                    self.info, self.status, self.listing = \
                        info, status, listing
                    self.status_at = time.time()
                    self._note_run(status)
                else:
                    self.info = info
            # 記録は状態のキャッシュを置いた後に読む。記録を書けなくても、装置の
            # 様子は正しく出す
            self._collect_logs()
            # 停止した時刻は、同じ回に受け取った停止したときの行にだけ使う。状態を
            # 読む前に停止したときの行を受け取っていたら、もう使う行はない。残すと、
            # 次の実行が始まるまでの間に受け取った異常停止に移った状態の行(実行の外の
            # 異常)に、前の実行の停止した時刻を付ける(_stamp_stop)
            with self._cache_lock:
                self._stop_stamp = 0.0
            # 読めなかった間の記録は、ここまでに受け取った
            self._missed = False
            self._ok()
        except (DeviceError, OSError) as e:
            self._fail(e, failure_text(e, self.cfg.get("host", "")))
        except Exception as e:   # noqa: BLE001  下の注釈のとおり
            # 想定外の例外(壊れた応答での KeyError など)でこのループが
            # 終わると、収集だけが警告なしに終了する。画面は最後に取れた値を
            # 出し続け、error も空のままなので誰も気づけない。
            # 理由を記録してループは続ける(notify.py・coupler.py と同じ扱い)
            self._fail(e, sentences("装置の状態を取得できません", unexpected_text(e)))
            if type(e) is not type(self._last_unexpected):
                traceback.print_exc()
            self._last_unexpected = e

    def _wait(self, seconds: float) -> None:
        """seconds 秒待つ。停止の指示には早めに気づくよう、0.05 秒ずつ見る。"""
        end = time.monotonic() + seconds
        while not self._stop and time.monotonic() < end:
            time.sleep(0.05)

    def _collect_logs(self) -> None:
        """装置の記録を、空になるまで読んで logs.jsonl へ書く。

        書けた分だけ装置に受け取りを知らせる。書けなかった記録は装置に残り、
        次の収集で読み直して書き直す。書けない理由は save_error に出す(装置との
        通信は正常なので、収集の失敗にはしない)。通信の失敗はそのまま上げる。
        """
        why = [w for w in (self._read_logs(),) if w]
        if self._console_id_unsaved:
            try:
                self._save_console_id()
            except (OSError, ValueError) as e:
                # ValueError は、手で書き換えた padcue.json が読めないとき
                # (project.ConfigError。文はどのファイルの何が悪いかを示す)
                why.append(sentences("接続している Switch の本体 ID を padcue.json に"
                                     "保存できません", error_text(e)))
        self.save_error = why[0] if why else ""

    def read_logs_now(self) -> None:
        """装置の記録を、収集の周期を待たずにいま読んで logs.jsonl へ書く。

        操作で装置が書いた記録を、その後に PC が書く行より先に並べるのに使う。記録の
        時刻は受け取った時刻なので、次の収集(最大 1 秒後)を待つと、後で書いた PC の
        行より後になる(開始が異常を解除したときの「異常停止 → 待機中」が、開始の行の
        後に並ぶ)。読めなければ、次の収集に任せる。書けなかった理由は、次の収集が
        出す(書けなかった記録は装置に残り、そのとき読み直す)。
        """
        try:
            self._read_logs()
        except (DeviceError, OSError):
            pass

    def _read_logs(self) -> str:
        """装置の記録を、空になるまで読んで logs.jsonl へ書く(_collect_logs の中身)。
        書けなければ、その理由の文を返す。通信の失敗はそのまま上げる。"""
        dev = self.registered_id
        reader, lock = self._log_cursors.get(dev)

        def keep(entries):
            self._note_requested_reconnect(entries)
            if reader.ack is None:
                # 始めて読む(サーバーを起動した直後など)。前の読み出す側が書いた後で
                # 受け取りを知らせていない記録を、もう一度書かない
                entries = unwritten(self.project, dev, entries)
            self._keep_logs(entries)

        began = time.time()
        try:
            with lock:
                reader.read(lambda ack: self.call(lambda c: c.logs(ack)), keep)
        except _NotKept as e:
            cause = e.__cause__
            # 書けなかったのでなければ、想定していない失敗である(_keep_logs)
            return sentences("装置のログを保存できません",
                             error_text(cause) if isinstance(cause, OSError)
                             else unexpected_text(cause))
        self._drained_at = began
        return ""

    def _note_requested_reconnect(self, entries: list) -> None:
        """指示した USB の再接続による切断に印を付ける(ログを赤くしない)。"""
        if time.time() - self.reconnect_at > RECONNECT_NOTE_S:
            return
        for e in entries:
            if e.get("kind") == "USB_UMOUNT":
                e["requested"] = True
                self.reconnect_at = 0.0
                return

    def _keep_logs(self, entries: list) -> None:
        """読んだ記録を書く。書けなければ _NotKept を上げる(通信の失敗と分ける)。

        想定外の例外(手で壊した、送った手順のコピーを読んだときなど)も、書けなかった
        ものとして扱う。収集の失敗にすると、受け取りを知らせないまま毎回同じ記録で
        失敗し、通信は正常なのに装置が「未検出」になる。
        """
        try:
            self._store_logs(entries)
        except Exception as e:
            raise _NotKept() from e

    def _store_logs(self, entries: list) -> None:
        """回収した記録を、装置の ID を付けて logs.jsonl へ書く。

        RUN_START は id の先頭 64bit(b/c)を PC の手順 id と照合して手順名に
        復元する(装置は名前を持たないので、中身から作った id の先頭 64bit を
        キーにする。扱う手順の数では、先頭が重なることはない)。"""
        self._stamp_unread(entries)
        self._stamp_stop(entries)
        # RUN_START がなければ、送った手順のコピーを読まない
        prefix2name = {}
        if any(e["kind"] == "RUN_START" for e in entries):
            prefix2name = {fid[:16]: n
                           for fid, n in self.project.sent.names().items()}
        for e in entries:
            if e["kind"] == "RUN_START":
                h = f"{int(e['b']):08x}{int(e['c']):08x}"
                if h in prefix2name:
                    e["name"] = prefix2name[h]
            elif e["kind"] == "HOST_INFO":
                # 本体識別子。「どの Switch につながっているか」の表示と、
                # 本体への命名(consoles)のキーに使う。登録情報へも残す
                # (_collect_logs)。USB を挿した瞬間にしか流れてこないので、
                # 覚えなければ画面を起動し直した時点で分からなくなる
                self._note_console_id(host_mac(e["a"], e["b"]))
        self.project.logs.append(entries, dev=self.registered_id)

    def _stamp_stop(self, entries: list) -> None:
        """停止したときの装置の行(停止した原因と、異常停止に移った状態の行など。
        runlog.lines_up_to_the_stop)を、受け取った時刻ではなく、収集が見積もった
        停止した時刻(run_ended_at)で書く。

        記録の終わりの行(PC_RUN_END)は、停止した時刻を付けて、約 2 秒後に書かれる。
        受け取った時刻のまま書くと、そのときにこれらの行が終わりの時刻に合わせられ
        (runlog の _before_the_end)、見ている間に行の時刻が戻る。

        収集は状態を読んでから記録を読むので、停止したときの行を受け取るときには、
        普通はもう停止した時刻を出している。状態を読んだ後に停止したときは、まだ
        出していないので、受け取った時刻のまま書く(終わりの行が来たときに合わせられる)。

        停止した時刻は、それを出した後の最初の停止したときの行にだけ使う。使わなくても、
        停止したときの行か実行の開始の行を受け取ったとき、収集の 1 回の終わり
        (_collect_once)、実行の始まりを状態で見たとき(_note_run)に捨てる。

        収集の 1 回の間に始まって停止した実行(準備の中の異常停止など)は、開始の行と
        停止したときの行を同じ回に受け取る。停止した時刻を付けるのは、その開始の行より
        後の行だけである。開始の行とそれより前の行(前の実行が停止した後の行かもしれ
        ない)は、受け取った時刻のまま書く(開始の行は画面が畳むので見えない)。
        """
        stops = [i for i, e in enumerate(entries) if runlog.stop_line(e)]
        starts = [i for i, e in enumerate(entries) if e.get("kind") == "RUN_START"]
        if not stops and not starts:
            return
        # 停止した時刻は、状態を読んだとき(収集と操作の書き戻し)に出す
        with self._cache_lock:
            at, self._stop_stamp = self._stop_stamp, 0.0
        if not at or not stops:
            return
        begun = max((i for i in starts if i < stops[-1]), default=-1)
        for i in runlog.lines_up_to_the_stop(entries):
            if i > begun:
                # 装置の時計から時刻を出した行(_stamp_unread)は、停止した時刻より後に
                # しない
                entries[i]["at"] = min(entries[i].get("at", at), at)

    def _stamp_unread(self, entries: list) -> None:
        """収集が装置を読めなかった間に装置が書いた行を、受け取った時刻ではなく、装置が
        書いた時刻で書く。

        読めない間(未検出の間など)の行は、接続し直してから受け取る。受け取った時刻で
        書くと、未検出の間に装置で起きたこと(合流点に着いて待ちに入ったなど)が、
        戻った後に起きたように並ぶ。装置の記録は、装置が起動してからの時計(t_ms)で
        書いた時を持つ。PC の時計に換算するための差は装置が返さないので、確かに言える
        上限を使う。行は受け取る前に書かれているので、差は「受け取った時刻 − t_ms」を
        超えない。
        実行の開始の行は、PC がその実行を初めて実行中と読んだ時刻より前に書かれている
        ので、差は「その時刻 − t_ms」も超えない(PC が送った開始なら、送った直後に
        読むので、ほぼ同じ時刻になる)。上限で出した時刻は、受け取った時刻より後には
        ならない。差は起動の印(boot)ごとに持つ。

        差の上限は、受け取った記録で縮め、実行の開始の行で置き直す。装置の時計と PC の
        時計は進み方が少し違うので、長く持った上限は今の差より小さくなりうる(出した
        時刻が、行を書いた時刻より前になる)。実行ごとに置き直し、さらに、前に記録を
        読み切った時刻(_drained_at)より前にはしない(その後に受け取る行は、どれもそれ
        より後に書かれている)。時刻を書き替えるのは、読めなかった間の後に受け取った
        記録(_missed)の、差を知っている起動の行だけである。

        装置の時計は一周する(実機は 32 ビットのミリ秒で約 49.7 日)。同じ起動の行の t_ms
        が前の行より大きく戻ったら一周したと見て、その起動の差の上限を捨て、その行から
        置き直す。一周する前に縮めた上限で変換すると、一周した後の行は約 49.7 日前の
        時刻になる。一周する前の行は、前の上限で変換する。
        """
        now = time.time()
        starts = [e for e in entries if e.get("kind") == "RUN_START"]
        # 差の上限は、起動と、その起動の時計が一周した回数ごとに持つ
        clocks = {(self._clock[0], 0): self._clock[1]} if self._clock else {}
        seen = ({self._clock[0]: (0, self._clock_t)}
                if self._clock and self._clock_t is not None else {})
        keys = {}                  # 行の位置 -> 差の上限の鍵
        last = None
        for i, e in enumerate(entries):
            t, boot = e.get("t_ms"), e.get("boot")
            if not isinstance(t, (int, float)) or boot is None:
                continue
            turn, before = seen.get(boot, (0, None))
            if before is not None and t < before - _CLOCK_WRAP_BACK_MS:
                turn += 1
            seen[boot] = (turn, t)
            key = keys[i] = (boot, turn)
            bound = now - t / 1000
            if starts and e is starts[-1] and self._began_open:
                # 実行中と読んだ後に初めて受け取った開始の行は、その実行のものである
                # (読む前に別の実行が始まって終わっていれば、その開始の行は前に並ぶ)
                self._began_open = False
                clocks[key] = min(bound, self._began_at - t / 1000)
            else:
                clocks[key] = min(clocks.get(key, bound), bound)
            last = key
        if last is not None:
            self._clock = (last[0], clocks[last])
            self._clock_t = seen[last[0]][1]
        if not self._missed:
            return
        for i, e in enumerate(entries):
            if i in keys:
                t = e["t_ms"]
                e["at"] = min(now, max(self._drained_at, t / 1000 + clocks[keys[i]]))
        self._mark_from_wait(entries)

    def _mark_from_wait(self, entries: list) -> None:
        """読めなかった間に装置が待ち(選択待ち、合流点)に入り、待ったまま読めるように
        なったら、最後に通った区切りを通った時刻(mark_at)を、待ちに入った状態の行の
        時刻(_stamp_unread が装置の時計から出した時刻)から出し直す。

        区切りを通った時刻は、状態を読んだ時刻から、区切りを通った後の経過フレーム数を
        引いて出す(_note_mark)。待ちの間は経過フレーム数が進まないので、戻って初めて
        読んだ区切りでは、待った間がまるごと抜ける。未検出の間に新しい周に入って
        待っていた装置の周の行の時刻(Coupler._note_laps)と、画面の待った長さと時間切れ
        までの残りは、この時刻から出す。収集は記録より先に状態を読むので、記録を受け
        取ったここで置き直す。監視はこの回の収集が終わるまで、このレーンを応答がないと
        見て、周の行を書かない(error をクリアするのは、この後の _ok である)。
        """
        began = [e for e in entries if e.get("kind") == "STATE"
                 and e.get("b") == _AWAITING_STATE and "at" in e]
        if not began:
            return
        with self._cache_lock:
            st = self.status
            lm = st.get("last_mark")
            fe = st.get("frames_elapsed")
            if not (st.get("awaiting") and lm and isinstance(fe, int)):
                return
            if (lm["index"], tuple(lm["counters"])) != self._mark_idx:
                return
            period_ns = (self.info.frame_period_ns if self.info
                         else proto.DEFAULT_FRAME_PERIOD_NS)
            at = began[-1]["at"] - max(0, fe - lm["frame"]) * period_ns / 1e9
            if at < self.mark_at:
                self.mark_at = at


class DevicePool:
    """装置の登録情報(設定)とリンク群を照合して管理する。"""

    def __init__(self, project, client_cls=None):
        self.project = project
        self.client_cls = client_cls
        self._links: dict[str, DeviceLink] = {}   # 名前 -> リンク
        self._order: list[str] = []               # 登録情報の並び
        self._lock = threading.Lock()             # _links の組み替え用
        # 登録情報を一度でも読んだか。最初に読んだときに作ったリンクだけが、
        # サーバーを起動したときからある装置である(DeviceLink.from_start)
        self._refreshed = False
        # 装置ごとの、記録をどこまで受け取ったかと、装置について覚えておく値。
        # 作り直したリンクが引き継ぐ
        self._log_cursors = LogCursors()
        self._notes = DeviceNotes()
        # 閉じた後は、リンクを作らない。閉じる前に始まっていた監視や要求が後から
        # refresh を呼んでも、閉じたプールが接続と収集のスレッドを作り直さない
        self._closed = False
        # 登録情報のファイルを読めない理由(読めれば空)。読めない間は、最後に
        # 読めたリンクの組のまま使う(refresh)
        self.config_error = ""

    def refresh(self) -> None:
        """設定を読み直し、リンクを生成・破棄・追従させる。

        装置の lock は取らない。無応答の装置の収集は、接続の待ち(最大 3 秒)の
        間 lock を持つ。画面の状態の取得も、どの装置の操作も、まずここを
        通るので、ここで待つとすべてがその待ちの後ろに並ぶ(要件 R13)。

        登録情報は、プールの lock の中で読む。外で読むと、先に読んだ古い内容が、
        後から読んだ新しい内容の反映の後に反映され、割り当ての解除や接続先の
        変更が戻る。

        読めないとき(手で書き換えて書き損じたとき)は、リンクの組を変えずに戻り、
        理由を config_error に覚える。停止の要求、収集、連動停止の監視は、最後に
        読めた組の接続で続ける。読めないからと停止の要求まで拒否すると、つながって
        いるのに停止できない。画面の状態は自分で登録情報を読み、理由を出す。
        """
        with self._lock:
            if self._closed:
                return
            try:
                cfg = self.project.load_config()
            except ConfigError as e:
                self.config_error = str(e)
                return
            self.config_error = ""
            devs = cfg.get("devices") or []
            self._order = [d.get("name", "") for d in devs]
            seen = set()
            first = not self._refreshed
            self._refreshed = True
            for d in devs:
                name = d.get("name", "")
                seen.add(name)
                link = self._links.get(name)
                if link is None:
                    link = DeviceLink(self.project, d, self.client_cls,
                                      self._log_cursors, self._notes)
                    link.from_start = first
                    self._links[name] = link
                    link.start()
                elif link.cfg != d:
                    # 登録情報の項目は接続先だけではない(名前、レーンの映像など)。
                    # どれが変わってもコピーを追従させる。接続先か装置 ID が
                    # 変わったときの古い接続は、次に lock を取った者が捨てる
                    # (DeviceLink._ensure)。装置 ID が変わったら、前の装置から
                    # 得た値も捨てる(解除してすぐ割り当て直すと、収集が未割り当ての
                    # 行を見ないまま次の装置につなぐ)
                    changed = link.cfg.get("id") != d.get("id")
                    link.cfg = dict(d)
                    if changed:
                        link.forget_device()
            for name in list(self._links):
                if name not in seen:
                    self._links.pop(name).close()

    def get(self, name: str = "", *, need_name: bool = False) -> DeviceLink:
        """操作対象のリンク。名前を省略すると登録情報の 1 台目である。

        need_name が真なら、装置が 2 台以上あるときは名前を省略できない。装置を
        動かす要求(手動操作・選択)が渡す。どれを動かすかを並び順で決めると、
        利用者が選んでいない装置が動く。1 台なら省略できる。

        登録情報のファイルを読めない間は、最後に読めた組から探し、見つからなければ
        読めないことを理由に拒否する(ConfigError。いまのファイルには、その名前が
        あるかもしれない)。
        """
        self.refresh()
        with self._lock:
            missing = not self._order or (name and name not in self._links)
            if missing and self.config_error:
                raise ConfigError(self.config_error)
            if not self._order:
                raise DeviceError("NO_HOST", "装置が登録されていません")
            if not name and need_name and len(self._order) >= 2:
                raise DeviceError("NO_TARGET", "操作する装置が指定されていません")
            key = name or self._order[0]
            link = self._links.get(key)
            if link is None:
                names = "・".join(self._order)
                raise DeviceError(
                    "NO_DEVICE", f"装置「{name}」は登録されていません"
                                 f"(登録済み: {names})")
            return link

    def links(self) -> list[DeviceLink]:
        """登録情報の並び順のリンク一覧(キャッシュ読み出し用)。"""
        self.refresh()
        with self._lock:
            return [self._links[n] for n in self._order if n in self._links]

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for link in self._links.values():
                link.close()
            self._links.clear()
            self._order = []
