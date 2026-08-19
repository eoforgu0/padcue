"""接続先の設定を壊せないこと。

接続先が空になると、以後どのコマンドも「接続先が未設定です」で止まり、
何が悪いのか分かりにくい状態になる(そうなった設定ファイルは手で直すしかない)。
"""
import pytest

from padcue import cli
from padcue.project import Project


@pytest.fixture
def root(tmp_path):
    p = Project(tmp_path)
    p.init_sample()
    return str(tmp_path)


def test_default_host_is_the_name(tmp_path):
    """設定ファイルが無いときは名前で呼ぶ(IP を調べる必要がない)。"""
    p = Project(tmp_path)
    assert not p.config_path.exists()
    assert p.load_config()["devices"][0]["host"] == "pademu.local"


def test_init_does_not_write_a_blank_host(root):
    """雛形作成が接続先を空で書き込まないこと。"""
    p = Project(root)
    assert p.load_config()["devices"][0].get("host")


def test_empty_address_is_refused(root, capsys):
    p = Project(root)
    cli.main(["--project", root, "device", "192.168.1.50"])
    assert cli.main(["--project", root, "device", ""]) == 1
    assert "空" in capsys.readouterr().out
    dev = p.load_config()["devices"][0]
    assert dev["host"] == "192.168.1.50", "空指定で上書きされた"


def test_whitespace_address_is_refused(root):
    p = Project(root)
    cli.main(["--project", root, "device", "pademu.local"])
    assert cli.main(["--project", root, "device", "   "]) == 1
    assert p.load_config()["devices"][0]["host"] == "pademu.local"


def test_address_is_trimmed(root):
    p = Project(root)
    assert cli.main(["--project", root, "device", "  10.0.0.9  "]) == 0
    assert p.load_config()["devices"][0]["host"] == "10.0.0.9"
