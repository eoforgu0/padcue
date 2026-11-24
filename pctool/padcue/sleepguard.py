"""OS のスリープの抑止。

どれかのレーンが「実行中」か「選択待ち」の間だけ、OS がスリープに入らない
ようにする。長時間の実行の途中でスリープに入ると、装置は動き続けるのに PC 側の
監視と記録が止まってしまう。画面の消灯は妨げない(見ていない間は消えてよい)
ので、ES_DISPLAY_REQUIRED は使わない。設定項目にはしない。

Windows では SetThreadExecutionState を使う。この状態は呼んだスレッドに
結び付くので、設定と解除はここの監視スレッド 1 本から行う。スレッドが終われば
OS 側の要求も消える。Windows 以外にこの仕組みはないので、何もしない。
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time
import traceback

# SetThreadExecutionState に渡す値(winbase.h)。ES_CONTINUOUS は「次に呼ぶまで
# この指定を続ける」、ES_SYSTEM_REQUIRED は「この PC をスリープに入れない」を
# 意味する。解除は ES_CONTINUOUS だけを渡して行う
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _windows() -> bool:
    """Windows かどうか。OS を見るのはここ 1 か所だけにする。"""
    return sys.platform == "win32"


def _set_execution_state(flags: int) -> None:
    # 失敗すると 0 が返る。黙って続けると、抑止できていないのに誰も気づかない
    if not ctypes.windll.kernel32.SetThreadExecutionState(flags):
        raise OSError("OS がスリープの抑止を受け付けませんでした")


def _busy(link) -> bool:
    from .devicepool import is_busy
    return is_busy(link.status)


class SleepGuard:
    """レーンの状態を見て、OS のスリープの抑止をオンかオフにする。"""

    POLL_S = 1.0        # 監視の周期。装置への問い合わせはない(収集の結果を読む)

    def __init__(self, pool, autostart: bool = True):
        self.pool = pool
        self._on = False        # いま抑止しているか
        self._stop = False
        # 監視が失敗した直近の理由(同じ理由でコンソールを埋めないため)
        self._error = ""
        self._thread = None
        # autostart=False はテスト用(tick を 1 回ずつ手で進めて規則を確認する)
        if autostart:
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="sleepguard")
            self._thread.start()

    def close(self) -> None:
        """監視を終わらせる。解除は監視スレッド自身が行う。"""
        self._stop = True

    def status(self) -> dict:
        """いま抑止しているかと、抑止できなかった理由(画面が出す)。"""
        return {"on": self._on, "error": self._error}

    def tick(self) -> None:
        try:
            links = self.pool.links()
        except ValueError:
            # 登録情報のファイルが読めない間は、レーンの状態が分からない。抑止は
            # 今のまま保ち、スリープの抑止の失敗とは表示しない(失敗と表示すると、
            # 利用者に Windows の電源の設定を変更させることになる)。読めない理由は、
            # 状態の取得と装置の操作が返す
            return
        self._apply(any(_busy(link) for link in links))

    def _apply(self, busy: bool) -> None:
        """抑止をオンかオフにする。変わったときだけ OS へ伝える。"""
        if busy == self._on:
            return
        # 覚えるのは OS が受け付けてから。先に覚えると、拒否されたときに次の
        # 周期で呼び直さない
        if _windows():
            _set_execution_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED if busy
                                 else ES_CONTINUOUS)
        self._on = busy

    def _loop(self) -> None:
        try:
            while not self._stop:
                try:
                    self.tick()
                    self._error = ""
                except Exception as e:   # noqa: BLE001  監視は続ける
                    # 捨ててしまうと、抑止が外れてスリープに入るようになった
                    # ことに誰も気づけない。同じ理由でコンソールが埋まらないよう、
                    # 変わったときだけ出す(notify.py と同じ扱い)
                    msg = f"{type(e).__name__}: {e}"
                    if msg != self._error:
                        self._error = msg
                        traceback.print_exc()
                time.sleep(self.POLL_S)
        finally:
            # 設定したのと同じスレッドで解除する(状態はスレッドに結び付く)
            self._apply(False)
