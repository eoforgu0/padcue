"""装置プール — GUI サーバの接続管理(2台同時運用の土台)。

契約(docs/design/multi-device-plan.md D3):
- **1装置 = 1接続 = 1装置lock**。状態収集(バックグラウンド)も操作(実行・停止・
  選択・手動操作)も、同じ接続を同じ lock の下で使う。実機は同時1接続で
  後から来た接続を優先する(横取り)ため、収集と操作を別の接続にすると
  奪い合いになって両方が壊れる
- lock の粒度は1コマンド。収集の合間に操作が必ず割り込める
- 収集は装置ごとに独立したスレッドで並列。結果はキャッシュに置き、
  /api/state はキャッシュを即答する。片方の無応答は、その装置の表示が
  「未接続」になるだけで、もう片方の表示・操作を一切妨げない(要件 R3)
- ログの回収はここに一本化する(装置側は読むと消えるため、読み手を1人に
  限らないと記録が散逸する。計画 D5)
"""
from __future__ import annotations

import threading
import time

from .client import DeviceClient, DeviceError, connect_verified


class DeviceLink:
    """1装置ぶんの接続・キャッシュ・収集スレッド。"""

    POLL_S = 1.0            # 収集周期(従来の /api/state 由来のポーリングと同じ)

    def __init__(self, project, devcfg: dict, client_cls=None):
        self.project = project
        self.cfg = dict(devcfg)          # {id, name, host, port}
        self.client_cls = client_cls or DeviceClient
        self.lock = threading.Lock()     # 接続と client への唯一の入口
        self.client = None
        # ---- キャッシュ(collector が書き、/api/state が読む) ----
        self.info = None                 # DeviceInfo(直近の HELLO)
        self.status: dict = {}           # 直近の STATUS
        self.listing: dict = {}          # 手順名 -> ハッシュ(直近の LIST)
        self.error: str = ""             # 直近の収集エラー(空 = 健康)
        self.error_exc: Exception | None = None   # 同・例外そのもの(表示整形用)
        self.host_info: str = ""         # つながっている本体の識別子(16進16桁)
        self.at: float = 0.0             # キャッシュ時刻(UNIX 秒)
        self.run_started_at: float = 0.0  # 実行中ならその開始時刻(0 = 実行なし)
        self._wt_at: float = 0.0         # 書き戻し(write-through)の時刻(単調時計)
        self._stop = False
        self._thread: threading.Thread | None = None

    # ---- 接続(必ず lock の下で) ----

    def _ensure(self):
        if self.client is not None and self.client.is_alive():
            return self.client
        self._drop()
        if not self.cfg.get("host"):
            raise DeviceError("NO_HOST", "接続先が未設定です")
        c, info = connect_verified(self.cfg, timeout=3.0,
                                   client_cls=self.client_cls)
        self.info = info
        self._learn(info)
        self.client = c
        return c

    def _drop(self) -> None:
        c, self.client = self.client, None
        if c is not None:
            try:
                c.close()
            except OSError:
                pass

    def _learn(self, info) -> None:
        """初回接続で個体IDを台帳へ控える(mock は控えない)。"""
        if self.cfg.get("id") or not info.device_id \
                or info.device_id.startswith("mock"):
            return
        cfg = self.project.load_config()
        for i, d in enumerate(cfg.get("devices", [])):
            if d.get("name") == self.cfg.get("name"):
                self.project.update_device(cfg, i, id=info.device_id)
                self.cfg["id"] = info.device_id
                return

    # ---- 操作(収集と同じ接続・同じ lock を通る唯一の窓口) ----

    def call(self, fn):
        """装置への1コマンド。接続が切れていたら1度だけ繋ぎ直してやり直す。

        TimeoutError は繋ぎ直さない: 相手が受け取り済みの操作を二重実行
        しかねないため、そのまま失敗として返す(従来 _retrying と同じ規則)。
        """
        with self.lock:
            try:
                return fn(self._ensure())
            except TimeoutError:
                self._drop()
                raise
            except DeviceError:
                raise                    # プロトコル上の拒否。接続は健康
            except (ConnectionError, OSError):
                self._drop()
                return fn(self._ensure())

    def write_through(self, status: dict | None = None,
                      listing: dict | None = None) -> None:
        """操作の直後に、その場で分かった最新値をキャッシュへ書き戻す。

        次の収集(最大1秒後)を待つと、転送直後の listing や停止直後の
        「実行中」表示が古いまま残る。時刻印を付け、操作より前にフェッチを
        始めていた収集がこの新しい値を古い結果で上書きしないようにする。
        """
        self._wt_at = time.monotonic()
        if status is not None:
            self.status = status
            self._note_run(status)
        if listing is not None:
            self.listing = listing

    def _note_run(self, status: dict) -> None:
        """実行の開始時刻を控える(画面の「終了予定」の表示に使う)。

        装置は「いつ始めたか」を持たず、経過フレーム数だけを返す。待機分岐で
        止まっている間はフレームが進まないので、経過から逆算した開始時刻は
        待った分だけ後ろへずれていく。そこで実行中になった瞬間を控える
        (操作の書き戻しでは即座に、外(CLI)からの実行は収集の周期ぶん=最大
        1秒の遅れで気づく。秒どまりの表示には足りる)。
        """
        on = bool(status.get("running") or status.get("awaiting"))
        if not on:
            self.run_started_at = 0.0
        elif not self.run_started_at:
            self.run_started_at = time.time()

    # ---- 収集 ----

    def start(self) -> None:
        if self._thread is not None:
            return
        # 本体識別子(HOST_INFO)は USB を挿した瞬間にしか流れてこない。
        # 起動時に保存済みログから拾い直しておかないと、GUI を立ち上げ
        # 直すたびに「次の抜き挿しまで不明」に戻ってしまう
        self._load_host_info()
        self._thread = threading.Thread(
            target=self._collect_loop, daemon=True,
            name=f"collect-{self.cfg.get('name', '?')}")
        self._thread.start()

    def _load_host_info(self) -> None:
        dev_id = self.cfg.get("id", "")
        if not dev_id:
            return
        try:
            for e in self.project.read_logs(2000):
                if e.get("dev") == dev_id and e.get("kind") == "HOST_INFO":
                    self.host_info = (f"{int(e.get('a', 0)):08x}"
                                      f"{int(e.get('b', 0)):08x}")
        except Exception:   # noqa: BLE001  記録が読めなくても収集は始める
            pass

    def stop(self) -> None:
        self._stop = True

    def close(self) -> None:
        self.stop()
        with self.lock:
            self._drop()

    def _collect_loop(self) -> None:
        while not self._stop:
            t0 = time.monotonic()
            try:
                # lock は1コマンドずつ取り直す(操作が合間に割り込めるように)
                info = self.call(lambda c: c.hello())
                status = self.call(lambda c: c.status())
                listing = {e["name"]: e["hash"]
                           for e in self.call(lambda c: c.list())}
                entries = self.call(lambda c: c.logs())
                if entries:
                    self._store_logs(entries, listing)
                if self._wt_at < t0:
                    # フェッチを始めた後に操作の書き戻しが入っていたら、
                    # こちらの(古い)結果でそれを上書きしない
                    self.info, self.status, self.listing = \
                        info, status, listing
                    self._note_run(status)
                else:
                    self.info = info
                self.error = ""
                self.error_exc = None
            except (DeviceError, ConnectionError, OSError) as e:
                self.error = str(e) or type(e).__name__
                self.error_exc = e
            self.at = time.time()
            # 1秒周期(処理時間ぶんは差し引く)。停止指示には早めに気づく
            wait = max(0.1, self.POLL_S - (time.monotonic() - t0))
            end = time.monotonic() + wait
            while not self._stop and time.monotonic() < end:
                time.sleep(0.05)
        with self.lock:
            self._drop()

    def _store_logs(self, entries: list, listing: dict) -> None:
        """回収したログを装置タグ付きで保存する(読むと装置側から消えるため、
        取り出した今しか帰属を付けられない)。RUN_START はハッシュ(b/c)を
        いま取った一覧と突き合わせて手順名に復元する。"""
        names = {h: n for n, h in listing.items()}
        for e in entries:
            if e.get("kind") == "RUN_START" and "c" in e:
                h = f"{int(e.get('b', 0)):08x}{int(e['c']):08x}"
                if h in names:
                    e["name"] = names[h]
            elif e.get("kind") == "HOST_INFO":
                # 本体識別子(USB ペアリング引数。実測で本体ごとに固有・
                # 安定を確認 2026-08-06)。「どの Switch につながっているか」
                # の表示に使う
                self.host_info = (f"{int(e.get('a', 0)):08x}"
                                  f"{int(e.get('b', 0)):08x}")
        dev_id = self.cfg.get("id") or getattr(self.client, "device_id", "")
        self.project.append_logs(entries, dev=dev_id)


class DevicePool:
    """装置台帳(設定)とリンク群を突き合わせて管理する。"""

    def __init__(self, project, client_cls=None):
        self.project = project
        self.client_cls = client_cls
        self._links: dict[str, DeviceLink] = {}   # 名前 -> リンク
        self._order: list[str] = []               # 台帳の並び
        self._lock = threading.Lock()             # _links の組み替え用

    def refresh(self) -> None:
        """設定を読み直し、リンクを生成・破棄・追従させる。"""
        cfg = self.project.load_config()
        devs = cfg.get("devices") or []
        with self._lock:
            self._order = [d.get("name", "") for d in devs]
            seen = set()
            for d in devs:
                name = d.get("name", "")
                seen.add(name)
                link = self._links.get(name)
                if link is None:
                    link = DeviceLink(self.project, d, self.client_cls)
                    self._links[name] = link
                    link.start()
                elif (link.cfg.get("host"), link.cfg.get("port"),
                      link.cfg.get("id")) != (d.get("host"), d.get("port"),
                                              d.get("id")):
                    # 接続先や個体IDが変わった。古い接続は捨てて追従する
                    with link.lock:
                        link.cfg = dict(d)
                        link._drop()
            for name in list(self._links):
                if name not in seen:
                    self._links.pop(name).close()

    def get(self, name: str = "") -> DeviceLink:
        """操作対象のリンク。名前省略時は台帳の1台目。"""
        self.refresh()
        with self._lock:
            if not self._order:
                raise DeviceError("NO_HOST", "装置が登録されていません")
            key = name or self._order[0]
            link = self._links.get(key)
            if link is None:
                names = " / ".join(self._order)
                raise DeviceError(
                    "NO_DEVICE", f"装置「{name}」は登録されていません"
                                 f"(登録済み: {names})")
            return link

    def links(self) -> list[DeviceLink]:
        """台帳の並び順のリンク一覧(キャッシュ読み出し用)。"""
        self.refresh()
        with self._lock:
            return [self._links[n] for n in self._order if n in self._links]

    def has_healthy(self, host: str, port: int) -> bool:
        """その宛先を健康な登録済みリンクが使用中か(探索の到達確認が
        自分の接続を横取りして壊すのを防ぐ)。"""
        with self._lock:
            return any(l.client is not None and not l.error
                       and (l.cfg.get("host"), int(l.cfg.get("port", 5555)))
                       == (host, int(port))
                       for l in self._links.values())

    def close(self) -> None:
        with self._lock:
            for link in self._links.values():
                link.close()
            self._links.clear()
            self._order = []
