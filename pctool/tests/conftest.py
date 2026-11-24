"""テストで共有する準備(fixture)。

pytest の慣習として、テストモジュールは互いに import しない。共有するものは、
fixture をここに、ただの関数とクラスを helpers.py に置く。テストモジュール
どうしで import し合うと、gcc ビルドの手順・skip の条件・出力先が 1 か所で
読めなくなる。
"""
from __future__ import annotations

import contextlib
import glob
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CORE = REPO / "firmware" / "components" / "pademu_core"
MAIN = REPO / "firmware" / "main"


def _localonly():
    """画面のテストのツールと同じ安全策(tools/_localonly.py)。"""
    import sys
    tools = str(REPO / "pctool" / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import _localonly
    return _localonly


@pytest.fixture(autouse=True, scope="session")
def _loopback_only():
    """テストの間、loopback 以外への接続・送信と、名前の解決を拒否する。

    テストは模擬装置だけを相手にする。登録情報に書いた接続先、探索のブロード
    キャスト、装置の名前(pademu-<下 4 桁>.local)のどれかが LAN へ出ると、同じ
    LAN にある実機が応答し、テストが実機を操作しうる。どの経路から来ても防げる
    ように、ソケットの段で拒否する(tools/_localonly.py の loopback_only)。
    このためテストの間は、探索は何も見つけずに終わり、名前でつなぐ試みは失敗する。
    テストはこれを前提にしてよい。

    テストの全体で 1 回だけ適用し、外さない。テストの後に残ったスレッド(収集や探索の
    監視)は、テストの全体が終わった後もプロセスが終わるまで動きうる。外すと、
    その間に LAN へ出る。
    """
    _localonly().loopback_only()


@pytest.fixture(autouse=True)
def _hide_this_pc(request, monkeypatch):
    """この PC のウィンドウ名・映像機器・画面を、テストに見せない。

    テストの失敗の出力に、開いているウィンドウの名前が含まれうる。画面のテストのツールと
    同じ差し替え(tools/_localonly.py の hide_this_pc)を適用する。映像ソースの実装
    そのものは test_sources.py が OS の呼び出しを差し替えて確認するので除く。
    映像機器の一覧を裏で探す動きはテストがあるので(test_gui_devices)、待ちは省略しない。
    """
    if request.node.path.name == "test_sources.py":
        return
    _localonly().hide_this_pc(monkeypatch.setattr, instant=False)


@pytest.fixture(autouse=True)
def _no_real_data(tmp_path_factory, monkeypatch):
    """--project を書かないコマンドが、本物の padcue のフォルダーの data を
    読み書きしないようにする。

    既定のプロジェクトは padcue のフォルダーの data(project.default_root)で、
    利用者が自分の padcue のフォルダーでテストを実行すると、そこに本物のデータがある。
    padcue のフォルダーを、テストごとの空のフォルダーに差し替える。
    """
    from padcue import project
    root = tmp_path_factory.mktemp("padcue")
    monkeypatch.setattr(project, "padcue_root", lambda: root)


@pytest.fixture(autouse=True)
def _fresh_device_list(monkeypatch):
    """映像機器の一覧のキャッシュ(sources.dshow_names)を、テストごとに空から始める。

    キャッシュはモジュールに 1 つあり、30 秒は取得し直さない。前のテストが差し替えた
    一覧が残ると、次のテストは自分が差し替えた一覧を見ない。
    """
    from padcue import sources
    monkeypatch.setattr(sources, "_device_list", sources._DeviceList())


@pytest.fixture
def proj(tmp_path):
    """ひな形(init_sample)を作ったプロジェクト。"""
    from padcue.project import Project

    p = Project(tmp_path)
    p.init_sample()
    return p


@pytest.fixture
def server(proj):
    """proj の操作画面のサーバー。渡すのは画面の URL である。"""
    from tests.helpers import gui_server

    with gui_server(proj) as base:
        yield base


class _Made:
    """serve() が作る LAN の探索(AutoScan)とスリープの抑止(SleepGuard)のダミー。

    作られたことだけを覚え、何もしない。画面が読む欄(候補と抑止の様子)は空で返す。
    """

    def __init__(self, made: list, *_args, **_kwargs):
        made.append(self)
        self.candidates: list = []

    def close(self) -> None:
        pass

    def status(self) -> dict:
        return {"on": False, "error": ""}


@pytest.fixture
def serve_stubs(monkeypatch):
    """本物の gui.serve() を呼ぶテストの準備。作られたダミーを種類ごとに返す。

    serve は、Windows の通知を出すか(os_toast)、通知から開く画面の所在(base_url)、
    LAN の探索(scan)、スリープの抑止(sleepguard)を、操作画面の class の属性に
    入れる。reset_handler_state はこの 4 つを戻さないので、元の値を保存してテストの後に
    戻す。戻さないと後のテストへ持ち越し、os_toast が立ったままになる。

    LAN の探索とスリープの抑止はダミーに替える。本物の探索は 15 秒ごとに LAN へ問い
    合わせ(loopback_only が拒否する)、本物の抑止は実行中のレーンがあると、この PC の
    スリープを実際に抑止する。Windows の通知も出さない。

    serve が作った操作画面のサーバー(gui._Server)も、made["server"] に作られた順に
    入る。serve は終了させる手段を返さずに serve_forever に入るので、テストはこれを
    受け取って shutdown する。
    """
    from padcue import gui
    for attr in ("os_toast", "base_url", "scan", "sleepguard"):
        monkeypatch.setattr(gui._Handler, attr, getattr(gui._Handler, attr))
    made: dict[str, list] = {"scan": [], "sleepguard": [], "server": []}
    real_server = gui._Server

    class Captured(real_server):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            made["server"].append(self)

    monkeypatch.setattr(gui, "_Server", Captured)
    monkeypatch.setattr(gui, "AutoScan",
                        lambda *a, **kw: _Made(made["scan"], *a, **kw))
    monkeypatch.setattr(gui, "SleepGuard",
                        lambda *a, **kw: _Made(made["sleepguard"], *a, **kw))
    monkeypatch.setattr(gui, "_toast_show", lambda *a, **kw: None)
    return made


@pytest.fixture
def mock_device():
    """早送りの倍率 100 の模擬装置。"""
    from padcue.mockdevice import MockDevice

    with MockDevice(speed=100.0) as d:
        yield d


@pytest.fixture
def owned_client(mock_device):
    """mock_device(早送りの倍率 100)につないで操作権を持った接続。"""
    from padcue.client import DeviceClient
    from tests.helpers import own

    with DeviceClient("127.0.0.1", mock_device.port, timeout=5.0) as c:
        own(c)
        yield c


@pytest.fixture
def fast_mock_device():
    """早送りの倍率 2000 の模擬装置。"""
    from padcue.mockdevice import MockDevice

    with MockDevice(speed=2000.0) as d:
        yield d


@pytest.fixture
def start_gui(tmp_path):
    """模擬装置と操作画面のサーバーを起動する関数。終了させるのはテストの終わりである。

    start_gui(*ids, speed=2000.0, procedures=None) は、tmp_path にひな形のプロジェクトを
    作り、procedures({手順名: 手順の文書})を procedures/ に書く。続けて ids の順に
    模擬装置を起動して 1P・2P… の名前で登録し、サーバーを起動する。返すのは
    (プロジェクト, [模擬装置...], 画面の URL) である。speed は模擬装置の
    早送りの倍率で、どの装置にも同じ値を使う。
    """
    from padcue.mockdevice import MockDevice
    from padcue.project import Project
    from tests.helpers import gui_server

    with contextlib.ExitStack() as stack:
        def start(*ids, speed=2000.0, procedures=None):
            proj = Project(tmp_path)
            proj.init_sample()
            for name, doc in (procedures or {}).items():
                (tmp_path / "procedures" / f"{name}.json").write_text(
                    json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            devices = []
            for device_id in ids:
                d = MockDevice(speed=speed, device_id=device_id)
                d.start()
                stack.callback(d.stop)
                devices.append(d)
            cfg = proj.load_config()
            cfg["devices"] = [{"id": d.device_id, "name": f"{n}P",
                               "host": "127.0.0.1", "port": d.port}
                              for n, d in enumerate(devices, 1)]
            proj.save_config(cfg)
            return proj, devices, stack.enter_context(gui_server(proj))
        yield start


def _find_gcc() -> str | None:
    """ホスト用の gcc を探す。PATH になければ、Windows の WinLibs の置き場所も見る。"""
    if shutil.which("gcc"):
        return "gcc"
    pattern = os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Packages"
        r"\BrechtSanders.WinLibs*\mingw64\bin\gcc.exe")
    hits = glob.glob(pattern)
    return hits[0] if hits else None


@pytest.fixture(scope="session")
def gcc() -> str:
    """ホスト用の gcc。なければ skip する。

    環境変数 PADCUE_REQUIRE_GCC があれば、skip せずに失敗させる。C 実装と Python の
    参照実装が同じ入力の列を出力することは、gcc がないと確認できない。skip のまま
    通ると、確認していないことに気づけない。CI はこの環境変数を設定して実行する。
    """
    found = _find_gcc()
    if found:
        return found
    if os.environ.get("PADCUE_REQUIRE_GCC"):
        pytest.fail("gcc が見つかりません(PADCUE_REQUIRE_GCC が立っています)")
    pytest.skip("gcc が見つかりません")


def _build_host(gcc: str, name: str, sources: list[Path],
                includes: list[Path]) -> Path:
    """ファームウェアの C をホストでビルドし、実行ファイルの場所を返す。

    出力先はリポジトリ直下の .hosttest/(非追跡)。%TEMP% に置くと、ウイルス
    対策ソフトが一時フォルダーに現れた署名のない実行ファイルを隔離して、それを使う
    テストがまとめて失敗することがある。build/ に置かないのは、そこが利用者の
    手順の変換結果を入れる場所だから(このリポジトリ自身をプロジェクトの
    フォルダーとして使える)。
    """
    out = REPO / ".hosttest" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    # 警告の厳しさはファームウェア本体(firmware/main/CMakeLists.txt)に合わせる。
    # ここだけ緩いと、実機のビルドで初めて失敗する
    cmd = [gcc, "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror"]
    for inc in includes:
        cmd += ["-I", str(inc)]
    cmd += [str(s) for s in sources]
    cmd += ["-o", str(out)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, f"C ビルド失敗:\n{res.stderr}"
    return out


@pytest.fixture(scope="session")
def core_host_exe(gcc: str) -> Path:
    """実行エンジン(pademu_core)のホスト版。"""
    return _build_host(gcc, "core_host.exe",
                       [CORE / "pademu_core.c", CORE / "host/core_host.c"],
                       [CORE / "include"])


@pytest.fixture(scope="session")
def procon_host_exe(gcc: str) -> Path:
    """Pro コントローラー互換の転送層のホスト版。"""
    return _build_host(gcc, "procon_host.exe",
                       [CORE / "pademu_procon.c", CORE / "pademu_tx.c",
                        CORE / "pademu_usb_desc.c", CORE / "host/procon_host.c"],
                       [CORE / "include"])


@pytest.fixture(scope="session")
def app_host_exe(gcc: str) -> Path:
    """状態機械(app_state.c)、操作権(app_owner.c)、起動のときのボタンの判定
    (app_button.c)、Wi-Fi の資格情報の選び方(app_wifi_creds.c)のホスト版。

    ESP-IDF の代わりの宣言(ロック、時刻、記録、NVS など)は firmware/main/host/include
    にあり、中身はハーネス(host/app_host.c)が持つ。その include を先に探させる
    ので、ESP-IDF のヘッダーは必要ない。
    """
    return _build_host(gcc, "app_host.exe",
                       [MAIN / "app_state.c", MAIN / "app_owner.c",
                        MAIN / "app_button.c", MAIN / "app_wifi_creds.c",
                        MAIN / "host/app_host.c"],
                       [MAIN / "host/include", MAIN, CORE / "include"])


@pytest.fixture(scope="session")
def engine_host_exe(gcc: str) -> Path:
    """実行エンジン(app_engine.c)のホスト版。タイマーは動かず、アラームの関数は
    ハーネス(host/engine_host.c)がテストの求めで呼ぶ。代わりの宣言は app_host_exe と
    同じ firmware/main/host/include にある。
    """
    return _build_host(gcc, "engine_host.exe",
                       [MAIN / "app_engine.c", CORE / "pademu_core.c",
                        MAIN / "host/engine_host.c"],
                       [MAIN / "host/include", MAIN, CORE / "include"])


@pytest.fixture(scope="session")
def store_host_exe(gcc: str) -> Path:
    """手順の保存(app_store.c)のホスト版。ハーネス(host/store_host.c)が app_store.c を
    読み込み、ファイルの操作と SPIFFS と SHA-256 を差し替える。代わりの宣言は
    app_host_exe と同じ firmware/main/host/include にある。
    """
    return _build_host(gcc, "store_host.exe", [MAIN / "host/store_host.c"],
                       [MAIN / "host/include", MAIN, CORE / "include"])


@pytest.fixture(scope="session")
def log_host_exe(gcc: str) -> Path:
    """記録のリング(app_log.c)のホスト版。ロックと時刻と乱数はハーネス
    (host/log_host.c)が持つ。代わりの宣言は app_host_exe と同じ
    firmware/main/host/include にある。
    """
    return _build_host(gcc, "log_host.exe",
                       [MAIN / "app_log.c", MAIN / "host/log_host.c"],
                       [MAIN / "host/include", MAIN, CORE / "include"])
