"""テストのツールと pytest が、本物の装置とこの PC の映像を使わないようにする安全策。

テストが装置へつなぎに行く経路は 2 つある。

- 探索。UDP のブロードキャストで LAN に問い合わせる(discover.discover。画面では
  autoscan が、コマンド行では cli が呼ぶ)
- 装置への接続。画面の装置プール(gui)、装置の追加(registry)、接続先の
  確認(autoscan)、コマンド行(cli)が DeviceClient でつなぐ。接続先には
  名前(pademu-<下 4 桁>.local)も書けるので、名前の解決も LAN へ出る

同じ LAN に実機があると、探索の応答や名前の解決が実機を指す。テストはその実機に
手順を転送し、つながっている Switch を実際に操作してしまう。

遮断の安全策は 3 重にする。どれか 1 つが漏れても、実機には何も送らない。

1. 探索の結果を模擬装置だけにする(pin_discovery)
2. DeviceClient の接続先が loopback でなければ遮断する(forbid_remote)
3. ソケットの段で、loopback 以外への接続と送信、名前の解決を遮断する
   (loopback_only)。1 と 2 は経路を名前で差し替えるので、経路が増えると
   漏れる。3 はどの経路から来ても遮断する

画面のテストのツールは lock_to_mock で 3 つとも当てる。pytest は、このうち 3 だけを
当てる(tests/conftest.py)。pytest には探索とコマンド行そのもののテストがあり、
1 と 2 で差し替えると、それらを確認できなくなるためである。探索の正しさは
tests/test_discover.py が loopback の上で確認する。

同じ理由で、この PC の映像機器・モニター・ウィンドウもテストに見せない
(hide_this_pc)。
"""
from __future__ import annotations

import errno
import ipaddress
import socket
import sys

from padcue import autoscan, gui, registry, sources
from padcue.discover import Found
from padcue.mockdevice import FW_VERSION

_LOCAL = ("127.0.0.1", "localhost", "::1")


def pin_discovery(port: int, device_id: str = "") -> None:
    """探索の結果を、loopback で待つ模擬装置 1 台だけにする。

    装置 ID も報告する。探索の結果から接続先を追いかけるのは、登録情報の装置 ID と
    一致する候補だけである(autoscan.AutoScan._follow)。報告しないダミーは、どの
    装置とも一致しない。
    """
    # バージョンは模擬装置の値をそのまま報告する(ここに転記すると、バージョンを上げた
    # 日から探索の結果だけ古いバージョンを返し続ける)
    fake = [Found(host="127.0.0.1", port=port, device_id=device_id,
                  fw=FW_VERSION)]
    autoscan.discover = lambda *a, **k: list(fake)


def forbid_remote() -> None:
    """DeviceClient が loopback 以外へつながないようにする。

    例外の種類は「つながらなかった」と同じ(DeviceError)にする。テストには
    わざとつながらないアドレスを入れて画面の表示を見る項目があり、そこだけ
    別の壊れ方をするとテスト自体が成り立たなくなるため。
    接続を遮断したことは端末に出す。
    """
    real = gui.DeviceClient

    class LocalOnlyClient(real):
        def __init__(self, host, port, *a, **kw):
            self._blocked = host not in _LOCAL
            if self._blocked:
                print(f"  [安全策] 実機への接続を遮断しました: {host}:{port}",
                      file=sys.stderr, flush=True)
            super().__init__(host, port, *a, **kw)

        def connect(self):
            if self._blocked:
                raise gui.DeviceError(
                    "LOCAL_ONLY",
                    "テスト中は loopback 以外へ接続しません(tools/_localonly.py)")
            return super().connect()

    gui.DeviceClient = LocalOnlyClient
    # つなぐ経路は画面だけではない。装置を追加する操作(registry)と、接続先を
    # 追いかける前の到達の確認(autoscan)も同じ方法で遮断する。
    # ここで漏れた経路は、loopback_only だけが遮断する
    registry.DeviceClient = LocalOnlyClient
    autoscan.DeviceClient = LocalOnlyClient


def _host(name) -> str:
    return name.decode("ascii", "replace") if isinstance(name, bytes) else name


def _is_literal(name) -> bool:
    """名前ではなく、数字で書いたアドレスか。"""
    try:
        ipaddress.ip_address(_host(name))
    except ValueError:
        return False
    return True


def _is_loopback(name) -> bool:
    """loopback の宛先か。名前は localhost だけを認める。"""
    name = _host(name)
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def _refused(what: str, name) -> str:
    """遮断したことを端末に出し、例外に入れる文を返す。

    この PC の名前は出さない(探索は自分のアドレスを知るために引く)。テストの
    出力は人に見せることがあり、PC の名前は使っている人の情報である。
    """
    shown = _host(name)
    if shown == socket.gethostname():
        shown = "この PC の名前"
    print(f"  [安全策] loopback 以外への{what}を遮断しました: {shown}",
          file=sys.stderr, flush=True)
    return f"テスト中は loopback 以外へ{what}をしません(tools/_localonly.py): {shown}"


def loopback_only() -> None:
    """ソケットが loopback 以外へ接続・送信し、名前を解決するのを遮断する。

    遮断するのは次の 3 種類である。
    - 接続(connect・connect_ex)と、宛先を付けた送信(sendto)。探索の
      ブロードキャストもここで遮断される
    - 名前の解決(getaddrinfo・gethostbyname・gethostbyname_ex)。通すのは
      localhost と、数字で書いたアドレスだけである。数字のアドレスは解決しても
      問い合わせが出ず、つなぐときに上のチェックを受ける
    - 逆引き(gethostbyaddr・getnameinfo)。通すのは loopback のアドレスだけである

    例外は OSError の仲間にする(名前の解決は socket.gaierror、逆引きは
    socket.herror)。つながらない宛先と同じ扱いになり、呼び出し側の失敗の経路が
    そのまま動く。遮断したことは端末に出す。

    元へ戻す手段は持たない。テストの後に残ったスレッドは、テストが終わった後も
    プロセスが終わるまで動きうるので、戻すとその間に LAN へ出る。pytest も
    戻さずに当てる(tests/conftest.py)。
    """
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_sendto = socket.socket.sendto
    real_getaddrinfo = socket.getaddrinfo
    real_byname = socket.gethostbyname
    real_byname_ex = socket.gethostbyname_ex
    real_byaddr = socket.gethostbyaddr
    real_nameinfo = socket.getnameinfo

    # アドレスが組でないソケット(AF_UNIX)はこの PC の中だけで閉じるので通す
    def remote(address) -> bool:
        return isinstance(address, tuple) and not _is_loopback(address[0])

    def connect(self, address):
        if remote(address):
            raise OSError(errno.ENETUNREACH, _refused("接続", address[0]))
        return real_connect(self, address)

    def connect_ex(self, address):
        if remote(address):
            _refused("接続", address[0])
            return errno.ENETUNREACH
        return real_connect_ex(self, address)

    def sendto(self, data, *args):
        if remote(args[-1]):
            raise OSError(errno.ENETUNREACH, _refused("送信", args[-1][0]))
        return real_sendto(self, data, *args)

    def lookup_allowed(name) -> bool:
        return name in (None, "", b"") or name == "localhost" or _is_literal(name)

    def getaddrinfo(host, *args, **kwargs):
        if not lookup_allowed(host):
            raise socket.gaierror(socket.EAI_NONAME, _refused("名前の解決", host))
        return real_getaddrinfo(host, *args, **kwargs)

    def gethostbyname(name):
        if not lookup_allowed(name):
            raise socket.gaierror(socket.EAI_NONAME, _refused("名前の解決", name))
        return real_byname(name)

    def gethostbyname_ex(name):
        if not lookup_allowed(name):
            raise socket.gaierror(socket.EAI_NONAME, _refused("名前の解決", name))
        return real_byname_ex(name)

    def gethostbyaddr(ip):
        if not _is_loopback(ip):
            raise socket.herror(errno.ENETUNREACH, _refused("逆引き", ip))
        return real_byaddr(ip)

    def getnameinfo(sockaddr, flags):
        if not (flags & socket.NI_NUMERICHOST) and not _is_loopback(sockaddr[0]):
            raise socket.gaierror(socket.EAI_NONAME, _refused("逆引き", sockaddr[0]))
        return real_nameinfo(sockaddr, flags)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.socket.sendto = sendto
    socket.getaddrinfo = getaddrinfo
    socket.gethostbyname = gethostbyname
    socket.gethostbyname_ex = gethostbyname_ex
    socket.gethostbyaddr = gethostbyaddr
    socket.getnameinfo = getnameinfo


def hide_this_pc(set_=setattr, instant: bool = True) -> None:
    """映像ソースの一覧をダミーにし、画面・ウィンドウ・映像機器から取得しなくする。

    テストは画面に出た文を集め、失敗した画面をスクリーンショットに残す。本物の一覧を
    通すと、開いているウィンドウの名前(閲覧中のページのタイトルなど)や画面の中身が、
    テストの記録に残る。手順や条件に本物の映像機器の名前が書いてあっても、
    その機器(OBS が開いているキャプチャーカード)を開かない。

    set_ は差し替えに使う関数(pytest では monkeypatch.setattr を渡して、テストの
    後に元へ戻す)。instant が真なら、映像機器の一覧を裏で探す待ちも省く。
    """
    monitors = [{"index": 1, "width": 1920, "height": 1080}]
    windows = [{"title": "テスト用のウィンドウ", "count": 1}]
    cameras = ["テスト用のカメラ"]
    # gui は名前で取り込んでいるので、両方を差し替える。名前から映像ソースを
    # 探す sources.find_source_by_name も同じ一覧を見る。映像機器の一覧は
    # sources のキャッシュ(dshow_names)が list_dshow_video_devices から取る
    for mod in (gui, sources):
        set_(mod, "list_monitors", lambda: list(monitors))
        set_(mod, "list_windows", lambda: list(windows))
    set_(sources, "list_dshow_video_devices", lambda: list(cameras))
    if instant:
        for mod in (gui, sources):
            set_(mod, "dshow_names", lambda wait=0.0: (list(cameras), False))

    def no_gdi():
        raise sources._CaptureError("テスト中は画面を取り込みません")

    def no_device(self):
        self.last_error = "テスト中は映像機器から取得しません"

    set_(sources, "_load_gdi", no_gdi)
    set_(sources.DShowSource, "_next_frame", no_device)


def lock_to_mock(port: int, device_id: str = "") -> None:
    """上の 4 つをまとめて適用する。画面のテストのツールはこれを呼ぶこと。"""
    pin_discovery(port, device_id)
    forbid_remote()
    loopback_only()
    hide_this_pc()
