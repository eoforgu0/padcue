"""検査道具が本物のマイコンに触れないようにするための歯止め。

uicheck.py / runbook_walk.py は「探す」を押し、見つかった相手に手順を転送して
実行する。本物のマイコンが同じ LAN にいると `pademu.local` がそちらへ解決する
ため、素通しにすると **実機が繋がっている Switch 2 を実際に操作してしまう**
(2026-07-31 に発生)。

対策は二重にする。
1. 探索の候補をモックだけに固定する(`pin_discovery`)
2. それでも取りこぼしたときのために、接続先が loopback 以外なら例外にする
   (`forbid_remote`)。1 が壊れても実機には届かない

探索そのものの正しさは tests/test_discover.py が見ているので、画面の検査で
本物の探索を走らせる必要はない。
"""
import sys

from padcue import gui
from padcue.discover import Found

_LOCAL = ("127.0.0.1", "localhost", "::1")


def pin_discovery(port: int) -> None:
    """「探す」の結果をモック(loopback)だけにする。"""
    gui.discover = lambda *a, **k: [
        Found(host="127.0.0.1", port=port, device_id="", fw="0.3.0-mock",
              how="探索")]


def forbid_remote() -> None:
    """loopback 以外へは、実際には一切繋がないようにする。

    例外の種類は「届かなかった」と同じ(DeviceError)にする。検査には
    わざと繋がらない住所を入れて画面の出方を見る項目があり、そこだけ
    別の壊れ方をすると検査自体が成り立たなくなるため。
    握りつぶしたことは端末に出す。
    """
    real = gui.DeviceClient

    class LocalOnlyClient(real):
        def __init__(self, host, port, *a, **kw):
            self._blocked = host not in _LOCAL
            if self._blocked:
                print(f"  [歯止め] 実機への接続を止めました: {host}:{port}",
                      file=sys.stderr, flush=True)
            super().__init__(host, port, *a, **kw)

        def connect(self):
            if self._blocked:
                raise gui.DeviceError(
                    "LOCAL_ONLY",
                    "検査中は loopback 以外へ接続しません(tools/_localonly.py)")
            return super().connect()

    gui.DeviceClient = LocalOnlyClient


def lock_to_mock(port: int) -> None:
    """上の2つをまとめて適用する。検査道具はこれを呼ぶこと。"""
    pin_discovery(port)
    forbid_remote()
