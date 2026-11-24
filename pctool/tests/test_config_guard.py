"""接続先に空の値を保存できないことと、登録情報の初期状態。

接続先が空になると、以後どのコマンドも「接続先が未設定です」で失敗し、
何が悪いのか分かりにくい状態になる(そうなった設定ファイルは手で編集するしかない)。
"""
import pytest

from padcue import cli
from padcue.project import Project


@pytest.fixture
def root(tmp_path):
    p = Project(tmp_path)
    p.init_sample()
    # 接続先を書き換えるテストなので、書き換える対象を 1 台置く。登録する
    # のは人の操作(device add)だけなので、テストでも自分で置く
    cfg = p.load_config()
    cfg["devices"] = [{"id": "aabbccddeeff", "name": "1P",
                       "host": "pademu.local", "port": 5555}]
    p.save_config(cfg)
    return str(tmp_path)


def test_a_new_config_has_no_device(tmp_path):
    """新しい登録情報は空で始まること。

    既定の接続先を 1 台分置いておくと、その宛先にいた装置が、人が選んで
    いないのに動く。同じ LAN に複数台あると、どれが動くのか分からないまま実行
    できてしまう。
    """
    p = Project(tmp_path)
    assert not p.config_path.exists()
    assert p.load_config()["devices"] == []


def test_the_config_is_not_written_just_by_reading(tmp_path):
    """読むだけでは登録情報を作らないこと。"""
    p = Project(tmp_path)
    p.load_config()
    assert not p.config_path.exists()


def test_empty_address_is_refused(root, capsys):
    p = Project(root)
    cli.main(["--project", root, "device", "192.0.2.50"])
    assert cli.main(["--project", root, "device", ""]) == 1
    assert "指定してください" in capsys.readouterr().err
    dev = p.load_config()["devices"][0]
    assert dev["host"] == "192.0.2.50", "空指定で上書きされた"


def test_whitespace_address_is_refused(root):
    p = Project(root)
    cli.main(["--project", root, "device", "pademu.local"])
    assert cli.main(["--project", root, "device", "   "]) == 1
    assert p.load_config()["devices"][0]["host"] == "pademu.local"


def test_address_is_trimmed(root):
    p = Project(root)
    assert cli.main(["--project", root, "device", "  192.0.2.9  "]) == 0
    assert p.load_config()["devices"][0]["host"] == "192.0.2.9"


def test_a_command_without_any_device_says_how_to_add_one(tmp_path, capsys):
    """登録情報が空のとき、次にすることが分かる文を出して終了すること。"""
    p = Project(tmp_path)
    p.init_sample()
    with pytest.raises(SystemExit) as caught:
        cli.main(["--project", str(tmp_path), "device", "192.0.2.9"])
    assert "device add" in str(caught.value)
