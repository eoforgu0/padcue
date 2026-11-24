"""LAN にいる装置を探し、登録情報の装置を追いかける監視。

やることは 2 つである。

- 登録情報にある装置の接続先が変わっていたら書き直す。装置の IP は無線の再接続で
  変わるので、これが「探す」の代わりになる。追いかけるのは装置 ID が一致する
  ものだけなので、動く装置が入れ替わることはない
- 未登録の装置は、候補として覚えるだけにする。登録するのは人である

登録することは「この装置を使う」という意思表示なので、人が選ぶ。
見つかった装置を自動で登録すると、同じネットワークに複数台あるときに、どれが動くのか
分からないまま実行でき、使うのをやめた装置も電源が入っていれば登録されて動いて
しまう。

模擬装置は候補にしない。デモ用のものを実機に混ぜて並べると、どれが本物か
画面から読めなくなる。模擬装置を使うときは、接続先を明示して登録する
(padcue-demo.bat か padcue device add 127.0.0.1)。

アドレスの手入力はコマンドだけで行う。画面に入力欄を置くと、探索で追いつける
はずのものを人が手で修正することになり、修正した値が次の探索で上書きされる。
"""
from __future__ import annotations

import threading
import time
import traceback

from .client import DeviceClient, DeviceError, is_mock
from .discover import discover
from .words import unexpected_text

# 探す周期。装置が起動して無線につながるまでの間は見つからないので、短すぎると
# 見つかるまでの待ちが長く感じられる。長すぎると、電源を入れてから一覧に出る
# までが遅い。1 回の探索そのものが 3 秒かかるので、その 5 倍を空ける
SCAN_S = 15.0


class AutoScan:
    """登録情報の装置を追いかけ、未登録の装置を候補として集める監視。"""

    def __init__(self, project, pool, *, timeout: float = 3.0,
                 autostart: bool = True):
        self.project = project
        self.pool = pool
        self.timeout = timeout
        # 未登録の装置。画面が「見つかった装置」として並べ、人が選ぶ。
        # 差し替えは丸ごと入れ替えるだけなので、読む側にロックは要らない
        self.candidates: list[dict] = []
        # 同じ理由でコンソールが埋まらないよう、変わったときだけ出す
        self.error = ""
        # 最後に探し終えた時刻(UNIX 時刻の秒)。画面が、探していることを示すのに使う
        self.last_at = 0.0
        self._stop = False
        self._thread: threading.Thread | None = None
        # autostart=False はテスト用(tick を手で 1 回ずつ進める)
        if autostart:
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="autoscan")
            self._thread.start()

    def close(self) -> None:
        self._stop = True

    def _loop(self) -> None:
        while not self._stop:
            try:
                self.tick()
                self.error = ""
                self.last_at = time.time()
            except Exception as e:   # noqa: BLE001  探索が失敗しても監視は続ける
                # 画面に出す文には、例外の型の名前と OS の英語を出さない。送れないのは
                # たいてい、PC がネットワークから切れているときである
                msg = ("ネットワークに接続されているかを確認してください"
                       if isinstance(e, OSError) else unexpected_text(e))
                if msg != self.error:
                    self.error = msg
                    traceback.print_exc()
            for _ in range(int(SCAN_S * 2)):
                if self._stop:
                    return
                time.sleep(0.5)

    def tick(self) -> list[str]:
        """1 周ぶん探す。登録情報の装置を追いかけ、残りを候補に置く。

        返すのは接続先が変わった装置の名前。
        """
        found = [f for f in discover(timeout=self.timeout)
                 if f.device_id and not is_mock(f.device_id)]
        cfg = self.project.load_config()
        devs = cfg.get("devices") or []
        known = {d.get("id") for d in devs if d.get("id")}
        # 未登録の装置は候補に置く。見つからなくなったら候補からも消えるので、
        # 毎回作り直す(電源を切った装置が候補に残り続けない)
        self.candidates = [{"id": f.device_id, "host": f.host, "port": f.port,
                            "fw": f.fw}
                           for f in found if f.device_id not in known]
        changed = []
        for f in found:
            name = self._follow(f, cfg, devs)
            if name:
                changed.append(name)
        if changed:
            self.pool.refresh()
        return changed

    def _follow(self, f, cfg, devs) -> str:
        """登録情報にいる同じ装置の接続先を、いまのアドレスに合わせる。

        一致を見るのは装置 ID だけである。アドレスで見立てると、IP を
        引き継いだ別の装置に乗り換えてしまう。
        """
        for i, d in enumerate(devs):
            if d.get("id") != f.device_id:
                continue
            if d.get("host") == f.host and int(d.get("port") or 0) == f.port:
                return ""
            # 接続先が変わった。IP は無線の再接続で変わるので追いかける。
            # ただし書き換える前に、そのアドレスで本当につながるか確認する。
            # 探索の応答は「受け取った経路のアドレス」で見えるので、PC に
            # 仮想アダプター(VPN や仮想マシン)があると、つながらない自分側の
            # アドレスが混じる。確認せずに書き換えると、いまつながっているのに
            # 未接続に変わってしまう。確認するのはアドレスが変わったときだけなので、
            # 平常は 1 本も余分につながない
            if not _reachable(f.host, f.port):
                return ""
            self.project.update_device(cfg, i, host=f.host, port=f.port)
            return d.get("name") or f.device_id
        return ""


def _reachable(host: str, port: int) -> bool:
    """そのアドレスで本当に装置が応答するか(短い待ちで確認する)。

    使用中の宛先でも試してよい。装置は制御の接続を 4 本まで受けるので、
    確認するための 1 本が収集の接続を壊すことはない。
    """
    c = DeviceClient(host, port, timeout=3.0)
    try:
        c.connect()
        return True
    except (DeviceError, OSError):
        return False
    finally:
        c.close()
