"""デモは実機を操作しない。実機と模擬装置を同じプロジェクトに登録しない。"""
import pytest

from padcue import cli, gui, registry
from padcue.mockdevice import MockDevice
from padcue.project import Project
from tests.helpers import get, post

MOCK_ID = "mock00000001"
REAL_ID = "00005e005301"


@pytest.fixture
def device():
    """登録の確認に答える装置を作る。ID で実機か模擬装置かが決まる。"""
    started = []

    def make(device_id):
        d = MockDevice(host="127.0.0.1", port=0, device_id=device_id)
        d.start()
        started.append(d)
        return d

    yield make
    for d in started:
        d.stop()


def registered(proj):
    return [d["id"] for d in proj.load_config()["devices"]]


def test_a_real_device_cannot_join_a_project_with_mock_devices(tmp_path, device):
    """模擬装置のいるプロジェクトに、実機を追加できないこと。"""
    proj = Project(tmp_path)
    assert registry.add_device(proj, "127.0.0.1", "1P", device(MOCK_ID).port).ok
    got = registry.add_device(proj, "127.0.0.1", "2P", device(REAL_ID).port)
    assert not got.ok
    assert "padcue.bat" in got.msg
    assert registered(proj) == [MOCK_ID]


def test_a_mock_device_cannot_join_a_project_with_real_devices(tmp_path, device):
    """実機のいるプロジェクトに、模擬装置を追加できないこと。"""
    proj = Project(tmp_path)
    assert registry.add_device(proj, "127.0.0.1", "1P", device(REAL_ID).port).ok
    assert not registry.add_device(proj, "127.0.0.1", "2P", device(MOCK_ID).port).ok
    assert registered(proj) == [REAL_ID]


def test_assigning_a_row_cannot_mix_real_and_mock(tmp_path, device):
    """割り当てを解除した行に、もう一方の種類の装置を割り当てられないこと。

    追加とは別の経路なので、追加の拒否だけでは混ざる。
    """
    proj = Project(tmp_path)
    first, second = device(MOCK_ID), device("mock00000002")
    assert registry.add_device(proj, "127.0.0.1", "1P", first.port).ok
    assert registry.add_device(proj, "127.0.0.1", "2P", second.port).ok
    assert registry.forget_device(proj, "2P")[0]
    got = registry.pick_device(proj, "2P", "127.0.0.1", device(REAL_ID).port)
    assert not got.ok
    assert registered(proj) == [MOCK_ID, ""]


def test_a_row_can_change_kind_when_nothing_else_is_registered(tmp_path, device):
    """ほかに装置がなければ、行に割り当てる装置の種類は問わないこと。"""
    proj = Project(tmp_path)
    assert registry.add_device(proj, "127.0.0.1", "1P", device(MOCK_ID).port).ok
    assert registry.forget_device(proj, "1P")[0]
    assert registry.pick_device(proj, "1P", "127.0.0.1", device(REAL_ID).port).ok
    assert registered(proj) == [REAL_ID]


def test_the_demo_refuses_registering_after_its_rows_are_removed(start_gui, device):
    """デモの起動では、模擬装置の行を削除した後も装置を登録できないこと。

    登録情報だけで判定すると、行を削除した後に実機を登録でき、次のデモの起動が
    失敗する。
    """
    proj, _devs, base = start_gui(MOCK_ID)
    gui._Handler.demo = True
    assert get(base, "/api/state")["demo"] is True
    name = proj.load_config()["devices"][0]["name"]
    assert post(base, "/api/device_remove", {"name": name}).get("ok")
    assert registered(proj) == []
    real = device(REAL_ID)
    for path, body in (("/api/device_add", {"name": "1P"}),
                       ("/api/device_pick", {"name": name})):
        got = post(base, path, {**body, "host": "127.0.0.1", "port": real.port})
        assert "padcue.bat" in got["error"], got
    assert registered(proj) == []


def test_starting_the_screen_refuses_a_registry_with_both_kinds(tmp_path, capsys):
    """手で編集して混ざった登録情報では、操作画面を起動しないこと。"""
    proj = Project(tmp_path)
    proj.save_config({"devices": [
        {"id": REAL_ID, "name": "1P", "host": "192.0.2.10", "port": 5555},
        {"id": MOCK_ID, "name": "2P", "host": "127.0.0.1", "port": 5556}]})
    assert cli.main(["--project", str(tmp_path), "gui", "--no-open"]) == 1
    assert "device remove" in capsys.readouterr().err
